// file_system: the file API of a mapped title, seen through the console's drives.
//
// A GDK title names its files on the console's drives, "GAME:\\data\\x.sps" being one in the installed game folder, so
// the imports it makes of CreateFile*, FindFirstFile*/FindNextFile*/FindClose, GetFileAttributes*/GetFileAttributesEx*,
// DeleteFile*, CreateDirectory*/RemoveDirectory*, MoveFile*, CopyFile*/CopyFile2 and SetFileAttributes* are answered
// here (docs/runtime_imports.md, "Console paths and the write overlay"; the prefixes come from ivall/xcl,
// GPL-2.0-only, as facts). The rules:
//   - "GAME:" (any case, "\\" or "/") maps under --game-dir, "G:\\" is the same folder and "T:\\" a scratch folder in
//     the overlay (RT-218); any other path is a host path and goes to the host;
//   - an open that can write, and DeleteFile, go to the overlay (--overlay, default <game-dir>\\overlay), so the
//     package stays exactly as installed. A write-open of a package file copies it into the overlay first, so
//     OPEN_EXISTING keeps its bytes; a delete of a package file leaves an empty marker under
//     <overlay>\\.gta6rt-deleted, and reads and listings treat the name as gone. The marker is never removed and a
//     deleted name is never copied up: a re-created file is its overlay copy, which reads and listings already
//     prefer, while an open of a deleted name that fails leaves the name deleted;
//   - reads prefer the overlay copy and then the package;
//   - FindFirstFile merges the two folders, the overlay winning on a name, and hides deleted names and the marker
//     folder;
//   - folders are made in the overlay; a removed package folder is hidden by a marker, a moved package file is
//     copied into the overlay and its old name deleted, and attributes are set on the overlay copy;
//   - a path that climbs above the root with ".." or names another drive is refused with ERROR_INVALID_NAME, so a
//     title cannot reach outside the package and the overlay.
// Only images mapped by the loader see these: they are bound through the registry, the runtime's own code calls the
// host directly.
#include <windows.h>

#include <cstring>
#include <cwchar>
#include <cwctype>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "file_system.h"
#include "shims.h"

namespace rt::fs {
namespace {

constexpr wchar_t kGamePrefix[] = L"game:";
constexpr size_t kGamePrefixLength = 5;
constexpr wchar_t kDeletedDir[] = L".gta6rt-deleted";
constexpr wchar_t kScratchDir[] = L".gta6rt-temp";  // what "T:\" names, inside the overlay
// A removed package folder is marked by this file inside its marker folder: the marker folder itself can already
// exist because files below it were deleted, and that alone does not hide the folder.
constexpr wchar_t kRemovedDirMarker[] = L".gta6rt-dir";
// Copy-up writes here first and then renames into place, so a half-copied file never sits under the title's name.
constexpr wchar_t kCopyDir[] = L".gta6rt-copy";

// The overlay root's own folders, which a title never sees in a listing.
bool internal_dir(const wchar_t* name) {
    return _wcsicmp(name, kDeletedDir) == 0 || _wcsicmp(name, kCopyDir) == 0 || _wcsicmp(name, kScratchDir) == 0;
}

std::mutex g_mutex;
std::filesystem::path g_game_dir;
std::filesystem::path g_overlay_dir;

struct Roots {
    std::filesystem::path game, overlay;
};

Roots roots() {
    std::lock_guard lock(g_mutex);
    return {g_game_dir, g_overlay_dir};
}

std::wstring lower(std::wstring s) {
    for (auto& c : s) c = static_cast<wchar_t>(std::towlower(c));
    return s;
}

bool same_name(const wchar_t* a, const wchar_t* b) { return _wcsicmp(a, b) == 0; }

// The narrow API (CreateFileA and friends) takes ANSI code page text, as the CRT does; the loader's own paths are
// handled as wide strings everywhere else.
std::wstring widen_ansi(const char* s) {
    if (!s) return {};
    const int n = MultiByteToWideChar(CP_ACP, 0, s, -1, nullptr, 0);
    if (n <= 1) return {};
    std::wstring w(static_cast<size_t>(n - 1), L'\0');
    MultiByteToWideChar(CP_ACP, 0, s, -1, w.data(), n);
    return w;
}

void narrow_ansi(const std::wstring& wide, char* out, size_t capacity) {
    if (capacity == 0) return;
    const int n = WideCharToMultiByte(CP_ACP, 0, wide.c_str(), -1, nullptr, 0, nullptr, nullptr);
    if (n <= 1 || static_cast<size_t>(n) > capacity) {
        out[0] = '\0';
        return;
    }
    WideCharToMultiByte(CP_ACP, 0, wide.c_str(), -1, out, static_cast<int>(capacity), nullptr, nullptr);
}

enum class Parsed { Host, Console, Refused };

// Splits a console path into the components below the root. A component that names a drive or a stream ("C:", "x:y")
// and a ".." that would leave the root are refused: the package and the overlay are the only folders a title may name.
bool is_separator(wchar_t c) { return c == L'\\' || c == L'/'; }

// True when `path` is `root` or lies below it, compared by name (case-insensitive, either separator).
bool lies_under(const std::wstring& path, const std::filesystem::path& root) {
    std::wstring base = lower(root.native());
    for (auto& c : base) if (c == L'/') c = L'\\';
    while (!base.empty() && base.back() == L'\\') base.pop_back();
    if (base.empty()) return false;
    std::wstring p = lower(path);
    for (auto& c : p) if (c == L'/') c = L'\\';
    return p.compare(0, base.size(), base) == 0 && (p.size() == base.size() || p[base.size()] == L'\\');
}

// "G:\x" is the installed package, like "GAME:\x", and "T:\x" a scratch folder the title may write to, the way the
// other console layers map those two drives (RT-218). Only the executable can say which spelling the title uses, so
// a host drive of the same letter must keep working: a path under --game-dir or the overlay (a package on drive G:,
// or a path GetModuleFileName handed back) stays a host path, and so does every G:/T: path while no roots are
// configured. The scratch folder lives in the overlay, as `.gta6rt-temp`.
enum class Drive { None, Game, Scratch };

Drive console_drive(const std::wstring& path, size_t& prefix_length) {
    if (path.size() >= kGamePrefixLength && lower(path.substr(0, kGamePrefixLength)) == kGamePrefix) {
        prefix_length = kGamePrefixLength;
        return Drive::Game;
    }
    if (path.size() < 2 || path[1] != L':' || (path.size() > 2 && !is_separator(path[2]))) return Drive::None;
    const wchar_t letter = static_cast<wchar_t>(std::towlower(path[0]));
    if (letter != L'g' && letter != L't') return Drive::None;
    const Roots r = roots();
    if (r.game.empty() && r.overlay.empty()) return Drive::None;
    if (lies_under(path, r.game) || lies_under(path, r.overlay)) return Drive::None;
    prefix_length = 2;
    return letter == L'g' ? Drive::Game : Drive::Scratch;
}

Parsed parse_console(const std::wstring& path, std::vector<std::wstring>& parts) {
    size_t prefix_length = 0;
    const Drive drive = console_drive(path, prefix_length);
    if (drive == Drive::None) return Parsed::Host;
    parts.clear();
    if (drive == Drive::Scratch) parts.push_back(kScratchDir);
    const size_t floor = parts.size();  // ".." may not climb out of the drive's own root
    std::wstring part;
    auto flush = [&]() -> bool {
        if (part.empty() || part == L".") {
            part.clear();
            return true;
        }
        if (part == L"..") {
            if (parts.size() <= floor) return false;
            parts.pop_back();
        } else if (part.find(L':') != std::wstring::npos) {
            return false;
        } else {
            parts.push_back(part);
        }
        part.clear();
        return true;
    };
    for (size_t i = prefix_length; i < path.size(); ++i) {
        const wchar_t c = path[i];
        if (is_separator(c)) {
            if (!flush()) return Parsed::Refused;
        } else {
            part.push_back(c);
        }
    }
    return flush() ? Parsed::Console : Parsed::Refused;
}

std::filesystem::path join(const std::filesystem::path& root, const std::vector<std::wstring>& parts) {
    std::filesystem::path p = root;
    for (const auto& part : parts) p /= part;
    return p;
}

struct Target {
    std::filesystem::path path;
    DWORD error = 0;  // non-zero: the caller fails with this error and does not touch the host
};

Target fail(DWORD error) {
    Target t;
    t.error = error;
    return t;
}

// The overlay keeps one empty marker per deleted package path, outside the mirrored tree so it cannot collide with a
// file the title creates.
std::filesystem::path marker(const std::filesystem::path& overlay, const std::vector<std::wstring>& parts) {
    return join(overlay / kDeletedDir, parts);
}

// A deleted file leaves an empty file at its marker path and a removed folder leaves kRemovedDirMarker inside its
// marker folder.
bool deleted(const std::filesystem::path& overlay, const std::vector<std::wstring>& parts) {
    if (overlay.empty() || parts.empty()) return false;
    std::error_code ec;
    // Only regular files are deletion markers; parent directories inside .gta6rt-deleted/ exist only to hold markers
    // for nested deleted files and must not hide the corresponding ancestor directories in the package.
    const std::filesystem::path m = marker(overlay, parts);
    return std::filesystem::is_regular_file(m, ec) || std::filesystem::is_regular_file(m / kRemovedDirMarker, ec);
}

// Writes an empty hidden marker file. On failure `error` holds the reason.
bool write_marker(const std::filesystem::path& mark, DWORD& error) {
    std::error_code ec;
    std::filesystem::create_directories(mark.parent_path(), ec);
    if (ec) {
        error = static_cast<DWORD>(ec.value());
        return false;
    }
    const HANDLE h = ::CreateFileW(mark.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                   nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_HIDDEN, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        error = ::GetLastError();
        return false;
    }
    ::CloseHandle(h);
    return true;
}

// Flushes a file the shim wrote on the title's behalf, for MOVEFILE_WRITE_THROUGH. On failure `error` holds the reason.
bool flush_file(const std::filesystem::path& path, DWORD& error) {
    const HANDLE h = ::CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                   nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        error = ::GetLastError();
        return false;
    }
    const BOOL ok = ::FlushFileBuffers(h);
    if (!ok) error = ::GetLastError();
    ::CloseHandle(h);
    return ok != FALSE;
}

// What a console path is in the title's view: the overlay copy if there is one, else the package's unless deleted.
enum class Kind { None, File, Directory };
struct View {
    Kind kind = Kind::None;
    bool in_overlay = false;  // the overlay holds the name
    bool in_package = false;  // the package holds the name and it is not deleted
};

View view(const Roots& r, const std::vector<std::wstring>& parts) {
    View v;
    std::error_code ec;
    if (parts.empty()) {  // "GAME:" itself
        v.kind = Kind::Directory;
        v.in_package = !r.game.empty();
        return v;
    }
    if (!r.overlay.empty()) {
        const std::filesystem::path p = join(r.overlay, parts);
        if (std::filesystem::exists(p, ec)) {
            v.in_overlay = true;
            v.kind = std::filesystem::is_directory(p, ec) ? Kind::Directory : Kind::File;
        }
    }
    if (!r.game.empty() && !deleted(r.overlay, parts)) {
        const std::filesystem::path p = join(r.game, parts);
        if (std::filesystem::exists(p, ec)) {
            v.in_package = true;
            if (v.kind == Kind::None) v.kind = std::filesystem::is_directory(p, ec) ? Kind::Directory : Kind::File;
        }
    }
    return v;
}

bool is_folder(const Roots& r, const std::vector<std::wstring>& parts) { return view(r, parts).kind == Kind::Directory; }

std::vector<std::wstring> parent_of(const std::vector<std::wstring>& parts) {
    return parts.empty() ? parts : std::vector<std::wstring>(parts.begin(), parts.end() - 1);
}

// Whether a folder has nothing in the title's view: no overlay entry and no package entry that is not deleted.
bool empty_in_view(const Roots& r, const std::vector<std::wstring>& parts) {
    std::error_code ec;
    if (!r.overlay.empty()) {
        const std::filesystem::path p = join(r.overlay, parts);
        for (auto it = std::filesystem::directory_iterator(p, ec); !ec && it != std::filesystem::directory_iterator();
             it.increment(ec)) {
            if (parts.empty() && internal_dir(it->path().filename().c_str())) continue;
            return false;
        }
    }
    ec.clear();
    if (!r.game.empty()) {
        const std::filesystem::path p = join(r.game, parts);
        for (auto it = std::filesystem::directory_iterator(p, ec); !ec && it != std::filesystem::directory_iterator();
             it.increment(ec)) {
            std::vector<std::wstring> child = parts;
            child.push_back(it->path().filename().wstring());
            if (!deleted(r.overlay, child)) return false;
        }
    }
    return true;
}

BOOL fail_bool(DWORD error) {
    SetLastError(error);
    return FALSE;
}

Target read_target(const std::wstring& path) {
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(path, parts);
    if (parsed == Parsed::Host) {
        Target t;
        t.path = std::filesystem::path(path);
        return t;
    }
    if (parsed == Parsed::Refused) return fail(ERROR_INVALID_NAME);

    const Roots r = roots();
    if (r.game.empty()) return fail(ERROR_PATH_NOT_FOUND);

    std::error_code ec;
    if (!r.overlay.empty()) {
        const std::filesystem::path overlay_path = join(r.overlay, parts);
        if (std::filesystem::exists(overlay_path, ec)) {
            Target t;
            t.path = overlay_path;
            return t;
        }
    }
    // A package file the title deleted is gone even though the package still holds it.
    if (deleted(r.overlay, parts)) return fail(ERROR_FILE_NOT_FOUND);

    Target t;
    t.path = join(r.game, parts);
    return t;
}

Target write_target(const std::wstring& path, bool copy_up) {
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(path, parts);
    if (parsed == Parsed::Host) {
        Target t;
        t.path = std::filesystem::path(path);
        return t;
    }
    if (parsed == Parsed::Refused) return fail(ERROR_INVALID_NAME);

    const Roots r = roots();
    if (r.overlay.empty()) return fail(ERROR_PATH_NOT_FOUND);
    if (parts.empty()) return fail(ERROR_INVALID_NAME);  // "GAME:" is a folder, not a file

    Target t;
    t.path = join(r.overlay, parts);

    std::error_code ec;
    std::filesystem::create_directories(t.path.parent_path(), ec);
    if (ec) {
        t.error = static_cast<DWORD>(ec.value());
        return t;
    }

    // A deleted package file is not there to copy: copying it up would bring the name back, and an open that keeps
    // the contents (OPEN_EXISTING, TRUNCATE_EXISTING) has to fail on it instead. CREATE_ALWAYS and CREATE_NEW still
    // make a new overlay file, which reads prefer over the package and so over the marker.
    const std::filesystem::path package_path = join(r.game, parts);
    // One copy-up at a time: a second thread writing the same file waits here and then finds the first one's copy, so
    // it neither fails on the taken name nor collides with that file's open through a rename of its own.
    static std::mutex s_copy_mutex;
    std::unique_lock copy_lock(s_copy_mutex, std::defer_lock);
    if (copy_up) copy_lock.lock();
    if (copy_up && !deleted(r.overlay, parts) && !std::filesystem::exists(t.path, ec) &&
        std::filesystem::is_regular_file(package_path, ec)) {
        // Copied to a private name, then renamed into place: a process killed mid-copy leaves only a hidden leftover,
        // not a truncated file that reads would prefer over the package.
        static unsigned s_copies = 0;  // under s_copy_mutex
        const std::filesystem::path staging = r.overlay / kCopyDir;
        std::filesystem::create_directories(staging, ec);
        const std::filesystem::path tmp =
            staging / (std::to_wstring(::GetCurrentProcessId()) + L"-" + std::to_wstring(++s_copies));
        if (!ec) std::filesystem::copy_file(package_path, tmp, std::filesystem::copy_options::overwrite_existing, ec);
        if (ec) {
            t.error = static_cast<DWORD>(ec.value());
            std::filesystem::remove(tmp, ec);
            return t;
        }
        if (!::MoveFileExW(tmp.c_str(), t.path.c_str(), 0)) {
            // The name taken means the title created the file itself meanwhile (CREATE_NEW and CREATE_ALWAYS don't
            // copy up, so they don't take the lock); that file is what it wrote, so keep it.
            const DWORD error = ::GetLastError();
            std::filesystem::remove(tmp, ec);
            if (!std::filesystem::exists(t.path, ec)) t.error = error;
        }
    }
    return t;
}

// MAXIMUM_ALLOWED names no right and grants every one the file allows, write included, so it is a write-open: opened
// as a read it handed out a handle to the package file itself, and a WriteFile through it changed the package.
bool writes(DWORD access, DWORD disposition) {
    constexpr DWORD kWriteAccess = GENERIC_WRITE | GENERIC_ALL | MAXIMUM_ALLOWED | FILE_WRITE_DATA | FILE_APPEND_DATA |
                                   FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES | DELETE | WRITE_DAC | WRITE_OWNER;
    if ((access & kWriteAccess) != 0) return true;
    return disposition == CREATE_NEW || disposition == CREATE_ALWAYS || disposition == OPEN_ALWAYS ||
           disposition == TRUNCATE_EXISTING;
}

Target open_target(const std::wstring& path, DWORD access, DWORD disposition) {
    if (!writes(access, disposition)) return read_target(path);
    // OPEN_ALWAYS and TRUNCATE_EXISTING read what is already there, and a write-open must not lose a package file's
    // bytes; CREATE_ALWAYS and CREATE_NEW replace it.
    const bool keeps_contents =
        disposition == OPEN_EXISTING || disposition == OPEN_ALWAYS || disposition == TRUNCATE_EXISTING;
    return write_target(path, keeps_contents);
}

// ---- FindFirstFile: the overlay and the package merged into one listing
//
// The host enumerates each folder; the results are merged here, overlay first, so a name that exists in both appears
// once with the overlay's times and size, a name the title deleted does not appear at all, and the overlay's marker
// folder never does.
struct Enumeration {
    std::vector<WIN32_FIND_DATAW> entries;
    size_t next = 0;
};

std::mutex g_enum_mutex;
std::map<HANDLE, std::unique_ptr<Enumeration>> g_enumerations;  // keyed by the pseudo-handle handed to the title

HANDLE register_enumeration(std::unique_ptr<Enumeration> e) {
    std::lock_guard lock(g_enum_mutex);
    const HANDLE h = reinterpret_cast<HANDLE>(e.get());
    g_enumerations.emplace(h, std::move(e));
    return h;
}

bool unregister_enumeration(HANDLE h) {
    std::lock_guard lock(g_enum_mutex);
    return g_enumerations.erase(h) != 0;
}

bool collect(const std::wstring& pattern, const std::vector<std::wstring>& parts, std::vector<WIN32_FIND_DATAW>& out,
             DWORD& error) {
    // "GAME:" and "GAME:\\dir\\" name the folder itself: list everything in it.
    std::vector<std::wstring> dirs;
    std::wstring leaf;
    const bool trailing_separator = !pattern.empty() && (pattern.back() == L'\\' || pattern.back() == L'/');
    if (parts.empty() || trailing_separator) {
        dirs = parts;
        leaf = L"*";
    } else {
        dirs.assign(parts.begin(), parts.end() - 1);
        leaf = parts.back();
    }

    const Roots r = roots();
    if (r.game.empty()) {
        error = ERROR_PATH_NOT_FOUND;
        return false;
    }

    auto add_root = [&](const std::filesystem::path& root, bool overlay_root) {
        if (root.empty()) return;
        std::filesystem::path search = join(root, dirs);
        search /= leaf;
        WIN32_FIND_DATAW found{};
        const HANDLE h = ::FindFirstFileW(search.c_str(), &found);
        if (h == INVALID_HANDLE_VALUE) return;
        do {
            if (overlay_root && internal_dir(found.cFileName)) continue;
            const bool dot = same_name(found.cFileName, L".") || same_name(found.cFileName, L"..");
            if (!overlay_root && !dot) {
                std::vector<std::wstring> entry_parts = dirs;
                entry_parts.push_back(found.cFileName);
                if (deleted(r.overlay, entry_parts)) continue;
            }
            bool duplicate = false;
            for (const auto& e : out)
                if (same_name(e.cFileName, found.cFileName)) {
                    duplicate = true;
                    break;
                }
            if (!duplicate) out.push_back(found);
        } while (::FindNextFileW(h, &found));
        ::FindClose(h);
    };
    add_root(r.overlay, true);
    add_root(r.game, false);

    if (out.empty()) {
        error = ERROR_FILE_NOT_FOUND;
        return false;
    }
    return true;
}

void to_ansi(const WIN32_FIND_DATAW& wide, WIN32_FIND_DATAA* ansi) {
    *ansi = WIN32_FIND_DATAA{};
    ansi->dwFileAttributes = wide.dwFileAttributes;
    ansi->ftCreationTime = wide.ftCreationTime;
    ansi->ftLastAccessTime = wide.ftLastAccessTime;
    ansi->ftLastWriteTime = wide.ftLastWriteTime;
    ansi->nFileSizeHigh = wide.nFileSizeHigh;
    ansi->nFileSizeLow = wide.nFileSizeLow;
    ansi->dwReserved0 = wide.dwReserved0;
    ansi->dwReserved1 = wide.dwReserved1;
    narrow_ansi(wide.cFileName, ansi->cFileName, MAX_PATH);
    narrow_ansi(wide.cAlternateFileName, ansi->cAlternateFileName, 14);
}

}  // namespace

void configure(const std::filesystem::path& game_dir, const std::filesystem::path& overlay_dir) {
    std::lock_guard lock(g_mutex);
    g_game_dir = game_dir;
    g_overlay_dir = overlay_dir;
}

std::filesystem::path game_dir() {
    std::lock_guard lock(g_mutex);
    return g_game_dir;
}

std::filesystem::path overlay_dir() {
    std::lock_guard lock(g_mutex);
    return g_overlay_dir;
}

HANDLE WINAPI create_file_w(LPCWSTR name, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES attributes,
                            DWORD disposition, DWORD flags, HANDLE template_file) {
    if (!name) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    const Target t = open_target(name, access, disposition);
    if (t.error) {
        SetLastError(t.error);
        return INVALID_HANDLE_VALUE;
    }
    return ::CreateFileW(t.path.c_str(), access, share, attributes, disposition, flags, template_file);
}

HANDLE WINAPI create_file_a(LPCSTR name, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES attributes,
                            DWORD disposition, DWORD flags, HANDLE template_file) {
    if (!name) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    const std::wstring wide = widen_ansi(name);
    return create_file_w(wide.c_str(), access, share, attributes, disposition, flags, template_file);
}

HANDLE WINAPI create_file2(LPCWSTR name, DWORD access, DWORD share, DWORD disposition,
                           LPCREATEFILE2_EXTENDED_PARAMETERS params) {
    if (!name) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    const Target t = open_target(name, access, disposition);
    if (t.error) {
        SetLastError(t.error);
        return INVALID_HANDLE_VALUE;
    }
    return ::CreateFile2(t.path.c_str(), access, share, disposition, params);
}

DWORD WINAPI get_file_attributes_w(LPCWSTR name) {
    if (!name) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_FILE_ATTRIBUTES;
    }
    const Target t = read_target(name);
    if (t.error) {
        SetLastError(t.error);
        return INVALID_FILE_ATTRIBUTES;
    }
    return ::GetFileAttributesW(t.path.c_str());
}

DWORD WINAPI get_file_attributes_a(LPCSTR name) {
    if (!name) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_FILE_ATTRIBUTES;
    }
    const std::wstring wide = widen_ansi(name);
    return get_file_attributes_w(wide.c_str());
}

BOOL WINAPI get_file_attributes_ex_w(LPCWSTR name, GET_FILEEX_INFO_LEVELS level, LPVOID info) {
    if (!name || !info) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    const Target t = read_target(name);
    if (t.error) {
        SetLastError(t.error);
        return FALSE;
    }
    return ::GetFileAttributesExW(t.path.c_str(), level, info);
}

BOOL WINAPI get_file_attributes_ex_a(LPCSTR name, GET_FILEEX_INFO_LEVELS level, LPVOID info) {
    if (!name || !info) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    const std::wstring wide = widen_ansi(name);
    return get_file_attributes_ex_w(wide.c_str(), level, info);
}

HANDLE WINAPI find_first_file_w(LPCWSTR pattern, LPWIN32_FIND_DATAW data) {
    if (!pattern || !data) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(pattern, parts);
    if (parsed == Parsed::Host) return ::FindFirstFileW(pattern, data);
    if (parsed == Parsed::Refused) {
        SetLastError(ERROR_INVALID_NAME);
        return INVALID_HANDLE_VALUE;
    }

    std::vector<WIN32_FIND_DATAW> entries;
    DWORD error = 0;
    if (!collect(pattern, parts, entries, error)) {
        SetLastError(error);
        return INVALID_HANDLE_VALUE;
    }
    auto e = std::make_unique<Enumeration>();
    e->entries = std::move(entries);
    e->next = 1;
    *data = e->entries[0];
    return register_enumeration(std::move(e));
}

HANDLE WINAPI find_first_file_ex_w(LPCWSTR pattern, FINDEX_INFO_LEVELS info_level, LPVOID data,
                                   FINDEX_SEARCH_OPS search_op, LPVOID search_filter, DWORD additional_flags) {
    if (!pattern || !data) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(pattern, parts);
    if (parsed == Parsed::Host)
        return ::FindFirstFileExW(pattern, info_level, data, search_op, search_filter, additional_flags);
    if (parsed == Parsed::Refused) {
        SetLastError(ERROR_INVALID_NAME);
        return INVALID_HANDLE_VALUE;
    }
    if (info_level != FindExInfoStandard && info_level != FindExInfoBasic) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    if (search_op != FindExSearchNameMatch && search_op != FindExSearchLimitToDirectories) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }

    std::vector<WIN32_FIND_DATAW> entries;
    DWORD error = 0;
    if (!collect(pattern, parts, entries, error)) {
        SetLastError(error);
        return INVALID_HANDLE_VALUE;
    }
    if (search_op == FindExSearchLimitToDirectories) {
        std::vector<WIN32_FIND_DATAW> dirs_only;
        for (auto& entry : entries)
            if ((entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) dirs_only.push_back(entry);
        entries = std::move(dirs_only);
        if (entries.empty()) {
            SetLastError(ERROR_FILE_NOT_FOUND);
            return INVALID_HANDLE_VALUE;
        }
    }
    if (info_level == FindExInfoBasic) {
        for (auto& entry : entries) entry.cAlternateFileName[0] = L'\0';
    }
    auto e = std::make_unique<Enumeration>();
    e->entries = std::move(entries);
    e->next = 1;
    *static_cast<LPWIN32_FIND_DATAW>(data) = e->entries[0];
    return register_enumeration(std::move(e));
}

HANDLE WINAPI find_first_file_ex_a(LPCSTR pattern, FINDEX_INFO_LEVELS info_level, LPVOID data,
                                   FINDEX_SEARCH_OPS search_op, LPVOID search_filter, DWORD additional_flags) {
    if (!pattern || !data) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    const std::wstring wide = widen_ansi(pattern);
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(wide, parts);
    if (parsed == Parsed::Host)
        return ::FindFirstFileExA(pattern, info_level, data, search_op, search_filter, additional_flags);
    WIN32_FIND_DATAW wide_data{};
    const HANDLE h = find_first_file_ex_w(wide.c_str(), info_level, &wide_data, search_op, search_filter, additional_flags);
    if (h != INVALID_HANDLE_VALUE) to_ansi(wide_data, static_cast<LPWIN32_FIND_DATAA>(data));
    return h;
}

HANDLE WINAPI find_first_file_a(LPCSTR pattern, LPWIN32_FIND_DATAA data) {
    if (!pattern || !data) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return INVALID_HANDLE_VALUE;
    }
    const std::wstring wide = widen_ansi(pattern);
    WIN32_FIND_DATAW found{};
    const HANDLE h = find_first_file_w(wide.c_str(), &found);
    if (h != INVALID_HANDLE_VALUE) to_ansi(found, data);
    return h;
}

BOOL WINAPI find_next_file_w(HANDLE find, LPWIN32_FIND_DATAW data) {
    if (!find || !data) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    {
        std::lock_guard lock(g_enum_mutex);
        const auto it = g_enumerations.find(find);
        if (it != g_enumerations.end()) {
            Enumeration& e = *it->second;
            if (e.next >= e.entries.size()) {
                SetLastError(ERROR_NO_MORE_FILES);
                return FALSE;
            }
            *data = e.entries[e.next++];
            SetLastError(ERROR_SUCCESS);
            return TRUE;
        }
    }
    return ::FindNextFileW(find, data);  // a host listing
}

BOOL WINAPI find_next_file_a(HANDLE find, LPWIN32_FIND_DATAA data) {
    if (!find || !data) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    WIN32_FIND_DATAW found{};
    const BOOL ok = find_next_file_w(find, &found);
    if (ok) to_ansi(found, data);
    return ok;
}

BOOL WINAPI find_close(HANDLE find) {
    if (!find) {
        SetLastError(ERROR_INVALID_HANDLE);
        return FALSE;
    }
    if (unregister_enumeration(find)) return TRUE;
    return ::FindClose(find);
}

BOOL WINAPI delete_file_w(LPCWSTR name) {
    if (!name) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(name, parts);
    if (parsed == Parsed::Host) return ::DeleteFileW(name);
    if (parsed == Parsed::Refused) {
        SetLastError(ERROR_INVALID_NAME);
        return FALSE;
    }

    const Roots r = roots();
    if (r.overlay.empty() || parts.empty()) {
        SetLastError(ERROR_INVALID_NAME);
        return FALSE;
    }

    bool removed = false;
    std::error_code ec;
    const std::filesystem::path overlay_path = join(r.overlay, parts);
    if (std::filesystem::exists(overlay_path, ec)) {
        if (!::DeleteFileW(overlay_path.c_str())) return FALSE;
        removed = true;
    }
    if (!r.game.empty() && !deleted(r.overlay, parts)) {
        const std::filesystem::path package_path = join(r.game, parts);
        if (std::filesystem::exists(package_path, ec)) {
            if (std::filesystem::is_directory(package_path, ec)) {
                SetLastError(ERROR_ACCESS_DENIED);
                return FALSE;
            }
            // The package file stays where it is; the marker is what hides it.
            DWORD error = 0;
            if (!write_marker(marker(r.overlay, parts), error)) return fail_bool(error);
            removed = true;
        }
    }
    if (!removed) {
        SetLastError(ERROR_FILE_NOT_FOUND);
        return FALSE;
    }
    return TRUE;
}

BOOL WINAPI delete_file_a(LPCSTR name) {
    if (!name) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    const std::wstring wide = widen_ansi(name);
    return delete_file_w(wide.c_str());
}

// ---- Folders: created in the overlay, removed by a marker when the package has them

namespace {

BOOL create_directory(LPCWSTR template_name, LPCWSTR name, LPSECURITY_ATTRIBUTES security) {
    if (!name) return fail_bool(ERROR_INVALID_PARAMETER);
    std::wstring template_path;
    if (template_name) {
        const Target t = read_target(template_name);
        if (t.error) return fail_bool(t.error);
        template_path = t.path.wstring();
    }
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(name, parts);
    if (parsed == Parsed::Host)
        return template_name ? ::CreateDirectoryExW(template_path.c_str(), name, security)
                             : ::CreateDirectoryW(name, security);
    if (parsed == Parsed::Refused) return fail_bool(ERROR_INVALID_NAME);

    const Roots r = roots();
    if (r.game.empty() || r.overlay.empty()) return fail_bool(ERROR_PATH_NOT_FOUND);
    if (view(r, parts).kind != Kind::None) return fail_bool(ERROR_ALREADY_EXISTS);  // "GAME:" included
    if (!is_folder(r, parent_of(parts))) return fail_bool(ERROR_PATH_NOT_FOUND);

    const std::filesystem::path target = join(r.overlay, parts);
    std::error_code ec;
    std::filesystem::create_directories(target.parent_path(), ec);
    if (ec) return fail_bool(static_cast<DWORD>(ec.value()));
    return template_name ? ::CreateDirectoryExW(template_path.c_str(), target.c_str(), security)
                         : ::CreateDirectoryW(target.c_str(), security);
}

}  // namespace

BOOL WINAPI create_directory_w(LPCWSTR name, LPSECURITY_ATTRIBUTES security) {
    return create_directory(nullptr, name, security);
}

BOOL WINAPI create_directory_a(LPCSTR name, LPSECURITY_ATTRIBUTES security) {
    if (!name) return fail_bool(ERROR_INVALID_PARAMETER);
    const std::wstring wide = widen_ansi(name);
    return create_directory(nullptr, wide.c_str(), security);
}

BOOL WINAPI create_directory_ex_w(LPCWSTR template_name, LPCWSTR name, LPSECURITY_ATTRIBUTES security) {
    if (!template_name) return fail_bool(ERROR_INVALID_PARAMETER);
    return create_directory(template_name, name, security);
}

BOOL WINAPI create_directory_ex_a(LPCSTR template_name, LPCSTR name, LPSECURITY_ATTRIBUTES security) {
    if (!template_name || !name) return fail_bool(ERROR_INVALID_PARAMETER);
    const std::wstring wide_template = widen_ansi(template_name), wide = widen_ansi(name);
    return create_directory(wide_template.c_str(), wide.c_str(), security);
}

BOOL WINAPI remove_directory_w(LPCWSTR name) {
    if (!name) return fail_bool(ERROR_INVALID_PARAMETER);
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(name, parts);
    if (parsed == Parsed::Host) return ::RemoveDirectoryW(name);
    if (parsed == Parsed::Refused) return fail_bool(ERROR_INVALID_NAME);

    const Roots r = roots();
    if (r.game.empty() || r.overlay.empty()) return fail_bool(ERROR_PATH_NOT_FOUND);
    if (parts.empty()) return fail_bool(ERROR_ACCESS_DENIED);  // "GAME:" itself stays
    const View v = view(r, parts);
    if (v.kind == Kind::None) return fail_bool(ERROR_FILE_NOT_FOUND);
    if (v.kind == Kind::File) return fail_bool(ERROR_DIRECTORY);
    if (!empty_in_view(r, parts)) return fail_bool(ERROR_DIR_NOT_EMPTY);

    if (v.in_overlay && !::RemoveDirectoryW(join(r.overlay, parts).c_str())) return FALSE;  // its error
    if (v.in_package) {
        // The package folder stays where it is; the marker hides it, and its files are already hidden one by one.
        DWORD error = 0;
        if (!write_marker(marker(r.overlay, parts) / kRemovedDirMarker, error)) return fail_bool(error);
    }
    return TRUE;
}

BOOL WINAPI remove_directory_a(LPCSTR name) {
    if (!name) return fail_bool(ERROR_INVALID_PARAMETER);
    const std::wstring wide = widen_ansi(name);
    return remove_directory_w(wide.c_str());
}

// ---- Move, copy and attributes: the result lands in the overlay, a moved package name is deleted by a marker

BOOL WINAPI move_file_ex_w(LPCWSTR from, LPCWSTR to, DWORD flags) {
    if (!from) return fail_bool(ERROR_INVALID_PARAMETER);
    std::vector<std::wstring> src, dst;
    const Parsed from_kind = parse_console(from, src);
    const Parsed to_kind = to ? parse_console(to, dst) : Parsed::Host;
    if (from_kind == Parsed::Host && to_kind == Parsed::Host) return ::MoveFileExW(from, to, flags);
    if (from_kind == Parsed::Refused || to_kind == Parsed::Refused) return fail_bool(ERROR_INVALID_NAME);
    // A boot-time move or delete is a host feature a title cannot use on the console's drives.
    if (!to || (flags & MOVEFILE_DELAY_UNTIL_REBOOT) != 0) return fail_bool(ERROR_INVALID_PARAMETER);
    // GAME: is its own volume: a move between it and a host path is refused the way a cross-volume move is.
    if (from_kind != to_kind) return fail_bool(ERROR_NOT_SAME_DEVICE);

    const Roots r = roots();
    if (r.game.empty() || r.overlay.empty()) return fail_bool(ERROR_PATH_NOT_FOUND);
    if (src.empty() || dst.empty()) return fail_bool(ERROR_ACCESS_DENIED);
    const View from_view = view(r, src);
    if (from_view.kind == Kind::None) return fail_bool(ERROR_FILE_NOT_FOUND);
    const View to_view = view(r, dst);
    if (to_view.kind == Kind::Directory) return fail_bool(ERROR_ACCESS_DENIED);
    if (to_view.kind == Kind::File && (flags & MOVEFILE_REPLACE_EXISTING) == 0) return fail_bool(ERROR_ALREADY_EXISTS);
    if (!is_folder(r, parent_of(dst))) return fail_bool(ERROR_PATH_NOT_FOUND);

    const std::filesystem::path overlay_from = join(r.overlay, src), overlay_to = join(r.overlay, dst);
    std::error_code ec;
    std::filesystem::create_directories(overlay_to.parent_path(), ec);
    if (ec) return fail_bool(static_cast<DWORD>(ec.value()));

    if (from_view.kind == Kind::Directory) {
        // A package folder is left where it is (as DeleteFile does); an overlay-only folder is renamed in place.
        if (from_view.in_package) return fail_bool(ERROR_ACCESS_DENIED);
        if (to_view.kind != Kind::None) return fail_bool(ERROR_ACCESS_DENIED);
        return ::MoveFileExW(overlay_from.c_str(), overlay_to.c_str(), flags & MOVEFILE_WRITE_THROUGH);
    }

    const bool write_through = (flags & MOVEFILE_WRITE_THROUGH) != 0;
    const DWORD keep = flags & (MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    DWORD error = 0;
    if (from_view.in_overlay) {
        if (!::MoveFileExW(overlay_from.c_str(), overlay_to.c_str(), keep | MOVEFILE_COPY_ALLOWED)) return FALSE;
    } else {
        // The host never sees a move here, only a copy and a marker, so MOVEFILE_WRITE_THROUGH is honoured by
        // flushing both before returning, as MoveFileEx does for its own copy and delete.
        const BOOL fail_if_exists = (flags & MOVEFILE_REPLACE_EXISTING) == 0;
        if (!::CopyFileW(join(r.game, src).c_str(), overlay_to.c_str(), fail_if_exists)) return FALSE;
        if (write_through && !flush_file(overlay_to, error)) return fail_bool(error);
    }
    if (from_view.in_package) {
        const std::filesystem::path mark = marker(r.overlay, src);
        if (!write_marker(mark, error)) return fail_bool(error);
        if (write_through && !flush_file(mark, error)) return fail_bool(error);
    }
    return TRUE;
}

BOOL WINAPI move_file_w(LPCWSTR from, LPCWSTR to) {
    if (!from || !to) return fail_bool(ERROR_INVALID_PARAMETER);
    return move_file_ex_w(from, to, MOVEFILE_COPY_ALLOWED);  // MoveFile moves files across volumes
}

BOOL WINAPI move_file_ex_a(LPCSTR from, LPCSTR to, DWORD flags) {
    if (!from) return fail_bool(ERROR_INVALID_PARAMETER);
    const std::wstring wide_from = widen_ansi(from), wide_to = to ? widen_ansi(to) : std::wstring();
    return move_file_ex_w(wide_from.c_str(), to ? wide_to.c_str() : nullptr, flags);
}

BOOL WINAPI move_file_a(LPCSTR from, LPCSTR to) {
    if (!from || !to) return fail_bool(ERROR_INVALID_PARAMETER);
    return move_file_ex_a(from, to, MOVEFILE_COPY_ALLOWED);
}

namespace {

// Where a copy writes: a host path as given; a GAME: path is checked against the title's view (an existing name
// counts even when only the package has it) and then means its overlay copy.
bool copy_destination(LPCWSTR to, bool fail_if_exists, std::filesystem::path& out, DWORD& error) {
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(to, parts);
    if (parsed == Parsed::Host) {
        out = std::filesystem::path(to);
        return true;
    }
    error = ERROR_INVALID_NAME;
    if (parsed == Parsed::Refused || parts.empty()) return false;
    const Roots r = roots();
    error = ERROR_PATH_NOT_FOUND;
    if (r.game.empty() || r.overlay.empty()) return false;
    const View v = view(r, parts);
    error = ERROR_ACCESS_DENIED;
    if (v.kind == Kind::Directory) return false;
    error = ERROR_FILE_EXISTS;
    if (v.kind == Kind::File && fail_if_exists) return false;
    error = ERROR_PATH_NOT_FOUND;
    if (!is_folder(r, parent_of(parts))) return false;
    out = join(r.overlay, parts);
    std::error_code ec;
    std::filesystem::create_directories(out.parent_path(), ec);
    if (ec) {
        error = static_cast<DWORD>(ec.value());
        return false;
    }
    error = 0;
    return true;
}

// The source and destination a copy opens, or the error it fails with.
bool copy_paths(LPCWSTR from, LPCWSTR to, bool fail_if_exists, std::filesystem::path& source,
                std::filesystem::path& destination, DWORD& error) {
    if (!from || !to) {
        error = ERROR_INVALID_PARAMETER;
        return false;
    }
    const Target t = read_target(from);
    if (t.error) {
        error = t.error;
        return false;
    }
    source = t.path;
    return copy_destination(to, fail_if_exists, destination, error);
}

}  // namespace

BOOL WINAPI copy_file_w(LPCWSTR from, LPCWSTR to, BOOL fail_if_exists) {
    std::filesystem::path source, destination;
    DWORD error = 0;
    if (!copy_paths(from, to, fail_if_exists != FALSE, source, destination, error)) return fail_bool(error);
    return ::CopyFileW(source.c_str(), destination.c_str(), fail_if_exists);
}

BOOL WINAPI copy_file_a(LPCSTR from, LPCSTR to, BOOL fail_if_exists) {
    if (!from || !to) return fail_bool(ERROR_INVALID_PARAMETER);
    const std::wstring wide_from = widen_ansi(from), wide_to = widen_ansi(to);
    return copy_file_w(wide_from.c_str(), wide_to.c_str(), fail_if_exists);
}

BOOL WINAPI copy_file_ex_w(LPCWSTR from, LPCWSTR to, LPPROGRESS_ROUTINE progress, LPVOID data, LPBOOL cancel,
                           DWORD flags) {
    std::filesystem::path source, destination;
    DWORD error = 0;
    if (!copy_paths(from, to, (flags & COPY_FILE_FAIL_IF_EXISTS) != 0, source, destination, error))
        return fail_bool(error);
    return ::CopyFileExW(source.c_str(), destination.c_str(), progress, data, cancel, flags);
}

BOOL WINAPI copy_file_ex_a(LPCSTR from, LPCSTR to, LPPROGRESS_ROUTINE progress, LPVOID data, LPBOOL cancel,
                           DWORD flags) {
    if (!from || !to) return fail_bool(ERROR_INVALID_PARAMETER);
    const std::wstring wide_from = widen_ansi(from), wide_to = widen_ansi(to);
    return copy_file_ex_w(wide_from.c_str(), wide_to.c_str(), progress, data, cancel, flags);
}

namespace {

// The host's CopyFile2, or null where the host has none. Wine 9's kernel32 and kernelbase don't export it: Wine's
// loader binds a static import of it to a stub that aborts the process when called ("unimplemented function
// KERNEL32.dll.CopyFile2, aborting"), so a direct ::CopyFile2 call (or MSVC's std::filesystem::copy_file, which uses
// it) kills a title under Wine 9 or a Proton built on it; pe_loader.cpp and loader_tls_test.cpp avoid it for the same
// reason. GetProcAddress returns null there instead, and the fallback below runs.
using CopyFile2Fn = HRESULT(WINAPI*)(PCWSTR, PCWSTR, COPYFILE2_EXTENDED_PARAMETERS*);
CopyFile2Fn host_copy_file2() {
    static const CopyFile2Fn fn = [] {
        for (const wchar_t* dll : {L"kernelbase.dll", L"kernel32.dll"}) {
            if (const HMODULE module = GetModuleHandleW(dll))
                if (const FARPROC p = GetProcAddress(module, "CopyFile2")) return reinterpret_cast<CopyFile2Fn>(p);
        }
        return CopyFile2Fn{};
    }();
    return fn;
}

// CopyFile2 through CopyFileExW, for hosts without it. The flags both calls define are passed on; the rest are
// CopyFile2-only hints and privilege requests (no offload, compressed traffic, low-free-space mode, ...) that don't
// change what a plain file copy produces, and CopyFileExW refuses flags it doesn't know. The cancel flag has the same
// type in both. A COPYFILE2 progress routine has another signature and message set, so it isn't called here.
HRESULT copy_file2_fallback(const wchar_t* source, const wchar_t* destination,
                            const COPYFILE2_EXTENDED_PARAMETERS* parameters) {
    constexpr DWORD kCopyFileExFlags = COPY_FILE_FAIL_IF_EXISTS | COPY_FILE_RESTARTABLE |
                                       COPY_FILE_OPEN_SOURCE_FOR_WRITE | COPY_FILE_ALLOW_DECRYPTED_DESTINATION |
                                       COPY_FILE_COPY_SYMLINK | COPY_FILE_NO_BUFFERING;
    const DWORD flags = parameters ? parameters->dwCopyFlags & kCopyFileExFlags : 0;
    BOOL* const cancel = parameters ? parameters->pfCancel : nullptr;
    if (::CopyFileExW(source, destination, nullptr, nullptr, cancel, flags)) return S_OK;
    const DWORD error = GetLastError();
    return HRESULT_FROM_WIN32(error ? error : ERROR_GEN_FAILURE);
}

}  // namespace

HRESULT WINAPI copy_file2(PCWSTR from, PCWSTR to, COPYFILE2_EXTENDED_PARAMETERS* parameters) {
    const bool fail_if_exists = parameters && (parameters->dwCopyFlags & COPY_FILE_FAIL_IF_EXISTS) != 0;
    std::filesystem::path source, destination;
    DWORD error = 0;
    if (!copy_paths(from, to, fail_if_exists, source, destination, error)) return HRESULT_FROM_WIN32(error);
    if (const CopyFile2Fn host = host_copy_file2()) return host(source.c_str(), destination.c_str(), parameters);
    return copy_file2_fallback(source.c_str(), destination.c_str(), parameters);
}

BOOL WINAPI set_file_attributes_w(LPCWSTR name, DWORD attributes) {
    if (!name) return fail_bool(ERROR_INVALID_PARAMETER);
    std::vector<std::wstring> parts;
    const Parsed parsed = parse_console(name, parts);
    if (parsed == Parsed::Host) return ::SetFileAttributesW(name, attributes);
    if (parsed == Parsed::Refused) return fail_bool(ERROR_INVALID_NAME);

    const Roots r = roots();
    if (r.game.empty() || r.overlay.empty()) return fail_bool(ERROR_PATH_NOT_FOUND);
    if (parts.empty()) return fail_bool(ERROR_ACCESS_DENIED);
    const View v = view(r, parts);
    if (v.kind == Kind::None) return fail_bool(ERROR_FILE_NOT_FOUND);

    // The attributes belong to the overlay copy: a package file is copied up and a package folder mirrored first,
    // so the package keeps its own attributes and reads, which prefer the overlay, see the new ones.
    const std::filesystem::path target = join(r.overlay, parts);
    if (!v.in_overlay) {
        std::error_code ec;
        if (v.kind == Kind::Directory) {
            std::filesystem::create_directories(target, ec);
            if (ec) return fail_bool(static_cast<DWORD>(ec.value()));
        } else {
            const Target t = write_target(name, true);
            if (t.error) return fail_bool(t.error);
        }
    }
    return ::SetFileAttributesW(target.c_str(), attributes);
}

BOOL WINAPI set_file_attributes_a(LPCSTR name, DWORD attributes) {
    if (!name) return fail_bool(ERROR_INVALID_PARAMETER);
    const std::wstring wide = widen_ansi(name);
    return set_file_attributes_w(wide.c_str(), attributes);
}

}  // namespace rt::fs

namespace {
using namespace rt::fs;

// CreateFile, the FindFirstFile family, the attribute queries and DeleteFile arrive through kernel32/kernelbase
// imports and through the api sets a GDK title is linked against (file-l1-1-0 for the find and attribute calls,
// file-l1-2-0 and file-l2-1-0 for CreateFile).
#define FILE_API(dll)                                                                                    \
    {dll, "CreateFileA", reinterpret_cast<void*>(&create_file_a)},                                       \
        {dll, "CreateFileW", reinterpret_cast<void*>(&create_file_w)},                                   \
        {dll, "CreateFile2", reinterpret_cast<void*>(&create_file2)},                                    \
        {dll, "FindFirstFileA", reinterpret_cast<void*>(&find_first_file_a)},                            \
        {dll, "FindFirstFileW", reinterpret_cast<void*>(&find_first_file_w)},                            \
        {dll, "FindFirstFileExA", reinterpret_cast<void*>(&find_first_file_ex_a)},                       \
        {dll, "FindFirstFileExW", reinterpret_cast<void*>(&find_first_file_ex_w)},                       \
        {dll, "FindNextFileA", reinterpret_cast<void*>(&find_next_file_a)},                              \
        {dll, "FindNextFileW", reinterpret_cast<void*>(&find_next_file_w)},                              \
        {dll, "FindClose", reinterpret_cast<void*>(&find_close)},                                        \
        {dll, "GetFileAttributesA", reinterpret_cast<void*>(&get_file_attributes_a)},                    \
        {dll, "GetFileAttributesW", reinterpret_cast<void*>(&get_file_attributes_w)},                    \
        {dll, "GetFileAttributesExA", reinterpret_cast<void*>(&get_file_attributes_ex_a)},               \
        {dll, "GetFileAttributesExW", reinterpret_cast<void*>(&get_file_attributes_ex_w)},               \
        {dll, "DeleteFileA", reinterpret_cast<void*>(&delete_file_a)},                                   \
        {dll, "DeleteFileW", reinterpret_cast<void*>(&delete_file_w)},                                   \
        {dll, "CreateDirectoryA", reinterpret_cast<void*>(&create_directory_a)},                         \
        {dll, "CreateDirectoryW", reinterpret_cast<void*>(&create_directory_w)},                         \
        {dll, "CreateDirectoryExA", reinterpret_cast<void*>(&create_directory_ex_a)},                    \
        {dll, "CreateDirectoryExW", reinterpret_cast<void*>(&create_directory_ex_w)},                    \
        {dll, "RemoveDirectoryA", reinterpret_cast<void*>(&remove_directory_a)},                         \
        {dll, "RemoveDirectoryW", reinterpret_cast<void*>(&remove_directory_w)},                         \
        {dll, "MoveFileA", reinterpret_cast<void*>(&move_file_a)},                                       \
        {dll, "MoveFileW", reinterpret_cast<void*>(&move_file_w)},                                       \
        {dll, "MoveFileExA", reinterpret_cast<void*>(&move_file_ex_a)},                                  \
        {dll, "MoveFileExW", reinterpret_cast<void*>(&move_file_ex_w)},                                  \
        {dll, "CopyFileA", reinterpret_cast<void*>(&copy_file_a)},                                       \
        {dll, "CopyFileW", reinterpret_cast<void*>(&copy_file_w)},                                       \
        {dll, "CopyFileExA", reinterpret_cast<void*>(&copy_file_ex_a)},                                  \
        {dll, "CopyFileExW", reinterpret_cast<void*>(&copy_file_ex_w)},                                  \
        {dll, "CopyFile2", reinterpret_cast<void*>(&copy_file2)},                                        \
        {dll, "SetFileAttributesA", reinterpret_cast<void*>(&set_file_attributes_a)},                    \
        {dll, "SetFileAttributesW", reinterpret_cast<void*>(&set_file_attributes_w)}
}  // namespace

SHIM_TABLE(file_system,
    FILE_API("kernel32.dll"),
    FILE_API("kernelbase.dll"),
    FILE_API("api-ms-win-core-file-l1-1-0.dll"),
    FILE_API("api-ms-win-core-file-l1-2-0.dll"),
    FILE_API("api-ms-win-core-file-l2-1-0.dll"),
);
