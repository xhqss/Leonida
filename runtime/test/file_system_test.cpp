// Checks the console path mapping the file shims install for a mapped title (RT-97).
//
// Every function is resolved through rt::shims::find under the DLL and name an import uses, so the test runs against
// the table the runtime installs rather than a private copy. It works on folders it creates in %TEMP%: a package
// folder that stands in for the game install, an overlay folder, and a "GAME:" path in each variant.
#include <windows.h>

#include <cstdio>
#include <filesystem>
#include <string>
#include <thread>
#include <vector>

#include "file_system.h"
#include "shims.h"

namespace {

using CreateFileWFn = HANDLE(WINAPI*)(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
using CreateFileAFn = HANDLE(WINAPI*)(LPCSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
using CreateFile2Fn = HANDLE(WINAPI*)(LPCWSTR, DWORD, DWORD, DWORD, LPCREATEFILE2_EXTENDED_PARAMETERS);
using FindFirstFileWFn = HANDLE(WINAPI*)(LPCWSTR, LPWIN32_FIND_DATAW);
using FindFirstFileAFn = HANDLE(WINAPI*)(LPCSTR, LPWIN32_FIND_DATAA);
using FindFirstFileExWFn =
    HANDLE(WINAPI*)(LPCWSTR, FINDEX_INFO_LEVELS, LPVOID, FINDEX_SEARCH_OPS, LPVOID, DWORD);
using FindFirstFileExAFn =
    HANDLE(WINAPI*)(LPCSTR, FINDEX_INFO_LEVELS, LPVOID, FINDEX_SEARCH_OPS, LPVOID, DWORD);
using FindNextFileWFn = BOOL(WINAPI*)(HANDLE, LPWIN32_FIND_DATAW);
using FindCloseFn = BOOL(WINAPI*)(HANDLE);
using GetFileAttributesWFn = DWORD(WINAPI*)(LPCWSTR);
using GetFileAttributesAFn = DWORD(WINAPI*)(LPCSTR);
using GetFileAttributesExWFn = BOOL(WINAPI*)(LPCWSTR, GET_FILEEX_INFO_LEVELS, LPVOID);
using DeleteFileWFn = BOOL(WINAPI*)(LPCWSTR);
using DeleteFileAFn = BOOL(WINAPI*)(LPCSTR);
using CreateDirectoryWFn = BOOL(WINAPI*)(LPCWSTR, LPSECURITY_ATTRIBUTES);
using CreateDirectoryAFn = BOOL(WINAPI*)(LPCSTR, LPSECURITY_ATTRIBUTES);
using CreateDirectoryExWFn = BOOL(WINAPI*)(LPCWSTR, LPCWSTR, LPSECURITY_ATTRIBUTES);
using RemoveDirectoryWFn = BOOL(WINAPI*)(LPCWSTR);
using RemoveDirectoryAFn = BOOL(WINAPI*)(LPCSTR);
using MoveFileWFn = BOOL(WINAPI*)(LPCWSTR, LPCWSTR);
using MoveFileExWFn = BOOL(WINAPI*)(LPCWSTR, LPCWSTR, DWORD);
using MoveFileAFn = BOOL(WINAPI*)(LPCSTR, LPCSTR);
using CopyFileWFn = BOOL(WINAPI*)(LPCWSTR, LPCWSTR, BOOL);
using CopyFileAFn = BOOL(WINAPI*)(LPCSTR, LPCSTR, BOOL);
using CopyFileExWFn = BOOL(WINAPI*)(LPCWSTR, LPCWSTR, LPPROGRESS_ROUTINE, LPVOID, LPBOOL, DWORD);
using CopyFile2Fn = HRESULT(WINAPI*)(PCWSTR, PCWSTR, COPYFILE2_EXTENDED_PARAMETERS*);
using SetFileAttributesWFn = BOOL(WINAPI*)(LPCWSTR, DWORD);
using SetFileAttributesAFn = BOOL(WINAPI*)(LPCSTR, DWORD);

int g_checks = 0;
int g_failures = 0;

void check(bool condition, const char* what) {
    ++g_checks;
    std::printf("%s %s\n", condition ? "PASS" : "FAIL", what);
    if (!condition) ++g_failures;
}

template <typename Fn>
Fn resolve(const char* dll, const char* name) {
    return reinterpret_cast<Fn>(rt::shims::find(dll, name));
}

void write_path(const std::filesystem::path& path, const std::string& text) {
    std::filesystem::create_directories(path.parent_path());
    const HANDLE h =
        ::CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    ::WriteFile(h, text.data(), static_cast<DWORD>(text.size()), &written, nullptr);
    ::CloseHandle(h);
}

std::string read_handle(HANDLE h) {
    std::string out;
    char buffer[256];
    DWORD got = 0;
    while (::ReadFile(h, buffer, sizeof(buffer), &got, nullptr) && got != 0) out.append(buffer, got);
    return out;
}

std::string read_path(const std::filesystem::path& path) {
    const HANDLE h = ::CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                   OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return {};
    std::string text = read_handle(h);
    ::CloseHandle(h);
    return text;
}

std::vector<std::wstring> list(FindFirstFileWFn first, FindNextFileWFn next, FindCloseFn close, const wchar_t* pattern) {
    std::vector<std::wstring> names;
    WIN32_FIND_DATAW found{};
    const HANDLE h = first(pattern, &found);
    if (h == INVALID_HANDLE_VALUE) return names;
    names.emplace_back(found.cFileName);
    while (next(h, &found)) names.emplace_back(found.cFileName);
    close(h);
    return names;
}

int count(const std::vector<std::wstring>& names, const wchar_t* name) {
    int n = 0;
    for (const auto& candidate : names)
        if (_wcsicmp(candidate.c_str(), name) == 0) ++n;
    return n;
}

bool contains(const std::vector<std::wstring>& names, const wchar_t* name) { return count(names, name) != 0; }

}  // namespace

int main() {
    const std::filesystem::path root =
        std::filesystem::temp_directory_path() / ("leonida_file_system_" + std::to_string(GetCurrentProcessId()));
    std::filesystem::remove_all(root);
    const std::filesystem::path package = root / "package";
    const std::filesystem::path overlay = root / "overlay";
    write_path(package / "data" / "hello.txt", "package");
    write_path(package / "data" / "dup.txt", "package");
    write_path(package / "data" / "bye.txt", "package");
    write_path(package / "data" / "sub" / "nested.txt", "nested");

    rt::fs::configure(package, overlay);
    check(rt::fs::game_dir() == package, "configure() sets the game folder");
    check(rt::fs::overlay_dir() == overlay, "configure() sets the overlay folder");

    // The table the runtime installs: kernel32 and the api sets a GDK title links against.
    const auto create_w = resolve<CreateFileWFn>("kernel32.dll", "CreateFileW");
    const auto create_a = resolve<CreateFileAFn>("kernelbase.dll", "CreateFileA");
    const auto first_w = resolve<FindFirstFileWFn>("api-ms-win-core-file-l1-1-0.dll", "FindFirstFileW");
    const auto first_a = resolve<FindFirstFileAFn>("kernel32.dll", "FindFirstFileA");
    const auto next_w = resolve<FindNextFileWFn>("kernel32.dll", "FindNextFileW");
    const auto find_close = resolve<FindCloseFn>("kernel32.dll", "FindClose");
    const auto attributes_w = resolve<GetFileAttributesWFn>("kernel32.dll", "GetFileAttributesW");
    const auto attributes_a = resolve<GetFileAttributesAFn>("kernel32.dll", "GetFileAttributesA");
    const auto attributes_ex_w = resolve<GetFileAttributesExWFn>("kernel32.dll", "GetFileAttributesExW");
    const auto delete_w = resolve<DeleteFileWFn>("kernel32.dll", "DeleteFileW");
    const auto delete_a = resolve<DeleteFileAFn>("api-ms-win-core-file-l1-1-0.dll", "DeleteFileA");
    check(create_w && create_a && first_w && first_a && next_w && find_close && attributes_w && attributes_a &&
              attributes_ex_w && delete_w && delete_a,
          "the file API resolves through the registry under kernel32/kernelbase and the api sets");

    // A read of a package file, through the mixed-case prefix and through forward slashes.
    {
        HANDLE h = create_w(L"GAME:\\data\\hello.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "CreateFileW opens GAME:\\data\\hello.txt from the package");
        check(read_handle(h) == "package", "the package file's bytes come back unchanged");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
        h = create_w(L"game:/data/hello.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "the prefix is case-insensitive and accepts forward slashes");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
        h = create_a("GAME:\\data\\hello.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "CreateFileA takes the same path");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
    }

    // Attributes, wide and narrow.
    check(attributes_w(L"GAME:\\data\\hello.txt") != INVALID_FILE_ATTRIBUTES, "GetFileAttributesW stats a package file");
    check(attributes_a("GAME:\\data\\hello.txt") != INVALID_FILE_ATTRIBUTES, "GetFileAttributesA stats a package file");
    const std::filesystem::path host_file = package / "data" / "hello.txt";
    check(attributes_w(host_file.c_str()) != INVALID_FILE_ATTRIBUTES, "a host path is passed through unchanged");

    // A write-open copies the package file into the overlay first, and the package is left alone.
    {
        const HANDLE h =
            create_w(L"GAME:\\data\\hello.txt", GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "a write-open of a package file succeeds");
        DWORD written = 0;
        if (h != INVALID_HANDLE_VALUE) {
            ::WriteFile(h, "overlay", 7, &written, nullptr);
            ::CloseHandle(h);
        }
        check(read_path(package / "data" / "hello.txt") == "package", "the package file is not modified");
        check(read_path(overlay / "data" / "hello.txt") == "overlay", "the write landed in the overlay copy");
        HANDLE r = create_w(L"GAME:\\data\\hello.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(r != INVALID_HANDLE_VALUE && read_handle(r) == "overlay", "reads prefer the overlay copy");
        if (r != INVALID_HANDLE_VALUE) ::CloseHandle(r);
    }

    // A new file, in folders the package does not have.
    {
        const HANDLE h = create_w(L"GAME:\\save\\slot1.bin", GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "CREATE_ALWAYS makes a new console file");
        DWORD written = 0;
        if (h != INVALID_HANDLE_VALUE) {
            ::WriteFile(h, "12345", 5, &written, nullptr);
            ::CloseHandle(h);
        }
        check(read_path(overlay / "save" / "slot1.bin") == "12345", "the new file is in the overlay");
        check(!std::filesystem::exists(package / "save"), "nothing was created in the package");

        WIN32_FILE_ATTRIBUTE_DATA data{};
        check(attributes_ex_w(L"GAME:\\save\\slot1.bin", GetFileExInfoStandard, &data) != FALSE &&
                  data.nFileSizeLow == 5,
              "GetFileAttributesExW reports the overlay copy's size");
    }

    // One listing, merged, overlay first and without duplicates.
    {
        const std::vector<std::wstring> names = list(first_w, next_w, find_close, L"GAME:\\data\\*");
        check(contains(names, L"hello.txt") && count(names, L"hello.txt") == 1, "the merged listing has hello.txt once");
        check(contains(names, L"dup.txt"), "the merged listing has the package's own files");
        check(contains(names, L"sub"), "the merged listing has the package's folders");
        check(!contains(names, L".gta6rt-deleted"), "the overlay's marker folder is not listed");

        const std::vector<std::wstring> root_names = list(first_w, next_w, find_close, L"GAME:\\");
        check(contains(root_names, L"data") && contains(root_names, L"save"),
              "GAME:\\ lists the package's folder and the overlay's");

        WIN32_FIND_DATAA ansi{};
        const HANDLE h = first_a("GAME:\\data\\*", &ansi);
        check(h != INVALID_HANDLE_VALUE && ansi.cFileName[0] != '\0', "FindFirstFileA fills an ANSI listing too");
        if (h != INVALID_HANDLE_VALUE) find_close(h);

        SetLastError(0);
        WIN32_FIND_DATAW none{};
        const HANDLE missing = first_w(L"GAME:\\missing\\*", &none);
        check(missing == INVALID_HANDLE_VALUE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "a pattern with no match reports ERROR_FILE_NOT_FOUND");
    }

    // A delete hides the package file without removing it.
    {
        check(delete_w(L"GAME:\\data\\dup.txt") != FALSE, "DeleteFileW accepts a package file");
        check(std::filesystem::exists(package / "data" / "dup.txt"), "the package file is still there");
        SetLastError(0);
        check(attributes_w(L"GAME:\\data\\dup.txt") == INVALID_FILE_ATTRIBUTES &&
                  GetLastError() == ERROR_FILE_NOT_FOUND,
              "the deleted name no longer resolves");
        SetLastError(0);
        const HANDLE h =
            create_w(L"GAME:\\data\\dup.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h == INVALID_HANDLE_VALUE && GetLastError() == ERROR_FILE_NOT_FOUND, "and cannot be opened");
        check(!contains(list(first_w, next_w, find_close, L"GAME:\\data\\*"), L"dup.txt"),
              "and is absent from the listing");

        // A write-open of the hidden name fails, and must leave the name deleted: no copy-up from the package, no
        // overlay file, and still hidden afterwards.
        SetLastError(0);
        const HANDLE write_open =
            create_w(L"GAME:\\data\\dup.txt", GENERIC_WRITE, 0, nullptr, OPEN_EXISTING, 0, nullptr);
        check(write_open == INVALID_HANDLE_VALUE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "a write-open of the deleted name fails with ERROR_FILE_NOT_FOUND");
        if (write_open != INVALID_HANDLE_VALUE) ::CloseHandle(write_open);
        check(!std::filesystem::exists(overlay / "data" / "dup.txt"), "the failed write-open left no overlay copy");
        SetLastError(0);
        check(attributes_w(L"GAME:\\data\\dup.txt") == INVALID_FILE_ATTRIBUTES &&
                  GetLastError() == ERROR_FILE_NOT_FOUND,
              "and the name is still deleted");

        // Re-creating it makes it readable again, and it is listed.
        const HANDLE again = create_w(L"GAME:\\data\\dup.txt", GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, 0, nullptr);
        DWORD written = 0;
        check(again != INVALID_HANDLE_VALUE, "the name can be created again");
        if (again != INVALID_HANDLE_VALUE) {
            ::WriteFile(again, "again", 5, &written, nullptr);
            ::CloseHandle(again);
        }
        check(read_path(overlay / "data" / "dup.txt") == "again", "the new copy is in the overlay");
        check(contains(list(first_w, next_w, find_close, L"GAME:\\data\\*"), L"dup.txt"),
              "and it is listed again");

        check(delete_a("GAME:\\data\\bye.txt") != FALSE, "DeleteFileA takes a console path");
        check(std::filesystem::exists(package / "data" / "bye.txt"), "the narrow delete leaves the package file too");
        SetLastError(0);
        check(delete_w(L"GAME:\\data\\bye.txt") == FALSE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "deleting an already hidden file reports ERROR_FILE_NOT_FOUND");
        SetLastError(0);
        check(delete_w(L"GAME:\\nope\\missing.txt") == FALSE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "deleting a file that never existed reports ERROR_FILE_NOT_FOUND");

        // Deleting a nested package file creates parent directories inside .gta6rt-deleted/; those marker parent
        // directories must not hide the corresponding ancestor directories in the package.
        check(delete_w(L"GAME:\\data\\sub\\nested.txt") != FALSE, "DeleteFileW accepts a nested package file");
        const DWORD sub_attrs = attributes_w(L"GAME:\\data\\sub");
        check(sub_attrs != INVALID_FILE_ATTRIBUTES && (sub_attrs & FILE_ATTRIBUTE_DIRECTORY) != 0,
              "deleting a nested package file does not hide its parent folder");
        check(contains(list(first_w, next_w, find_close, L"GAME:\\data\\*"), L"sub"),
              "the parent folder stays listed after deleting a nested package file");
    }

    // Deleting one package file must not hide its folder. The overlay has never written to GAME:\pkgonly, so the
    // folder is only in the package, and its marker folder under .gta6rt-deleted is not a marker for the folder.
    {
        write_path(package / "pkgonly" / "a.txt", "package");
        write_path(package / "pkgonly" / "b.txt", "package");
        check(delete_w(L"GAME:\\pkgonly\\a.txt") != FALSE, "DeleteFileW hides a file in a package-only folder");
        const DWORD folder = attributes_w(L"GAME:\\pkgonly");
        check(folder != INVALID_FILE_ATTRIBUTES && (folder & FILE_ATTRIBUTE_DIRECTORY) != 0,
              "the folder of a deleted package file still resolves");
        check(contains(list(first_w, next_w, find_close, L"GAME:\\*"), L"pkgonly"),
              "and is still in its parent's listing");
        check(contains(list(first_w, next_w, find_close, L"GAME:\\pkgonly\\*"), L"b.txt"),
              "and its other files are still listed");
        const HANDLE h =
            create_w(L"GAME:\\pkgonly\\b.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "and its other files still open");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
    }

    // Folders, moves, copies and attributes: the same mapping, through the names a title imports.
    const auto mkdir_w = resolve<CreateDirectoryWFn>("api-ms-win-core-file-l1-1-0.dll", "CreateDirectoryW");
    const auto mkdir_a = resolve<CreateDirectoryAFn>("kernel32.dll", "CreateDirectoryA");
    const auto mkdir_ex_w = resolve<CreateDirectoryExWFn>("kernel32.dll", "CreateDirectoryExW");
    const auto rmdir_w = resolve<RemoveDirectoryWFn>("api-ms-win-core-file-l1-1-0.dll", "RemoveDirectoryW");
    const auto rmdir_a = resolve<RemoveDirectoryAFn>("kernelbase.dll", "RemoveDirectoryA");
    const auto move_w = resolve<MoveFileWFn>("kernel32.dll", "MoveFileW");
    const auto move_ex_w = resolve<MoveFileExWFn>("api-ms-win-core-file-l2-1-0.dll", "MoveFileExW");
    const auto move_a = resolve<MoveFileAFn>("kernel32.dll", "MoveFileA");
    const auto copy_w = resolve<CopyFileWFn>("kernel32.dll", "CopyFileW");
    const auto copy_a = resolve<CopyFileAFn>("kernel32.dll", "CopyFileA");
    const auto copy_ex_w = resolve<CopyFileExWFn>("api-ms-win-core-file-l2-1-0.dll", "CopyFileExW");
    const auto copy2 = resolve<CopyFile2Fn>("api-ms-win-core-file-l2-1-0.dll", "CopyFile2");
    const auto setattr_w = resolve<SetFileAttributesWFn>("api-ms-win-core-file-l1-1-0.dll", "SetFileAttributesW");
    const auto setattr_a = resolve<SetFileAttributesAFn>("kernel32.dll", "SetFileAttributesA");
    check(mkdir_w && mkdir_a && mkdir_ex_w && rmdir_w && rmdir_a && move_w && move_ex_w && move_a && copy_w &&
              copy_a && copy_ex_w && copy2 && setattr_w && setattr_a,
          "the folder, move, copy and attribute calls resolve through the registry");

    write_path(package / "misc" / "one.txt", "one");
    write_path(package / "misc" / "two.txt", "two");
    write_path(package / "misc" / "three.txt", "three");
    write_path(package / "misc" / "inner" / "deep.txt", "deep");
    write_path(package / "misc" / "keep.txt", "keep");

    // CreateDirectory: the new folder is in the overlay; names the title can already see are refused.
    {
        check(mkdir_w(L"GAME:\\cache", nullptr) != FALSE, "CreateDirectoryW makes GAME:\\cache");
        check(std::filesystem::is_directory(overlay / "cache"), "in the overlay");
        check(!std::filesystem::exists(package / "cache"), "and not in the package");
        const DWORD a = attributes_w(L"GAME:\\cache");
        check(a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY) != 0, "the new folder resolves");
        check(contains(list(first_w, next_w, find_close, L"GAME:\\*"), L"cache"), "and is listed");
        check(mkdir_a("GAME:\\cache\\narrow", nullptr) != FALSE && std::filesystem::is_directory(overlay / "cache" / "narrow"),
              "CreateDirectoryA takes a console path");
        check(mkdir_ex_w(L"GAME:\\misc", L"GAME:\\cache\\from_template", nullptr) != FALSE &&
                  std::filesystem::is_directory(overlay / "cache" / "from_template"),
              "CreateDirectoryExW maps both the template and the new folder");
        SetLastError(0);
        check(mkdir_w(L"GAME:\\misc", nullptr) == FALSE && GetLastError() == ERROR_ALREADY_EXISTS,
              "a folder only the package has already exists");
        check(!std::filesystem::exists(overlay / "misc"), "and nothing is mirrored for it");
        SetLastError(0);
        check(mkdir_w(L"GAME:\\misc\\one.txt", nullptr) == FALSE && GetLastError() == ERROR_ALREADY_EXISTS,
              "a package file's name already exists");
        SetLastError(0);
        check(mkdir_w(L"GAME:\\", nullptr) == FALSE && GetLastError() == ERROR_ALREADY_EXISTS, "GAME:\\ already exists");
        SetLastError(0);
        check(mkdir_w(L"GAME:\\nowhere\\child", nullptr) == FALSE && GetLastError() == ERROR_PATH_NOT_FOUND,
              "a folder whose parent does not exist reports ERROR_PATH_NOT_FOUND");
        check(!std::filesystem::exists(overlay / "nowhere"), "and leaves no parent behind");
        check(mkdir_w(L"GAME:\\misc\\made", nullptr) != FALSE && std::filesystem::is_directory(overlay / "misc" / "made"),
              "a new folder inside a package folder lands in the overlay");
    }

    // RemoveDirectory: an overlay folder goes; a package folder is hidden by a marker once it is empty.
    {
        check(rmdir_w(L"GAME:\\cache\\narrow") != FALSE && !std::filesystem::exists(overlay / "cache" / "narrow"),
              "RemoveDirectoryW removes an empty overlay folder");
        SetLastError(0);
        check(rmdir_w(L"GAME:\\cache\\narrow") == FALSE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "removing it again reports ERROR_FILE_NOT_FOUND");
        SetLastError(0);
        check(rmdir_w(L"GAME:\\misc\\inner") == FALSE && GetLastError() == ERROR_DIR_NOT_EMPTY,
              "a package folder with files is not empty");
        SetLastError(0);
        check(rmdir_w(L"GAME:\\misc\\keep.txt") == FALSE && GetLastError() == ERROR_DIRECTORY,
              "a file is not a folder");
        SetLastError(0);
        check(rmdir_w(L"GAME:\\") == FALSE && GetLastError() == ERROR_ACCESS_DENIED, "GAME:\\ cannot be removed");
        check(delete_w(L"GAME:\\misc\\inner\\deep.txt") != FALSE, "once its file is deleted");
        check(rmdir_a("GAME:\\misc\\inner") != FALSE, "RemoveDirectoryA removes the now empty package folder");
        check(std::filesystem::exists(package / "misc" / "inner" / "deep.txt"), "the package keeps the folder and file");
        SetLastError(0);
        check(attributes_w(L"GAME:\\misc\\inner") == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_FILE_NOT_FOUND,
              "the removed folder no longer resolves");
        check(!contains(list(first_w, next_w, find_close, L"GAME:\\misc\\*"), L"inner"), "and is not listed");
        const DWORD misc = attributes_w(L"GAME:\\misc");
        check(misc != INVALID_FILE_ATTRIBUTES && (misc & FILE_ATTRIBUTE_DIRECTORY) != 0, "its parent is still there");
        check(mkdir_w(L"GAME:\\misc\\inner", nullptr) != FALSE, "the removed name can be made again");
        check(list(first_w, next_w, find_close, L"GAME:\\misc\\inner\\*").size() == 2,
              "and the new folder is empty: only . and ..");
    }

    // MoveFile: the destination is in the overlay; a moved package file is deleted by a marker.
    {
        check(move_w(L"GAME:\\misc\\one.txt", L"GAME:\\misc\\moved.txt") != FALSE, "MoveFileW moves a package file");
        check(read_path(overlay / "misc" / "moved.txt") == "one", "its bytes are in the overlay under the new name");
        check(read_path(package / "misc" / "one.txt") == "one", "the package file is not modified");
        SetLastError(0);
        check(attributes_w(L"GAME:\\misc\\one.txt") == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_FILE_NOT_FOUND,
              "the old name is gone");
        SetLastError(0);
        check(move_ex_w(L"GAME:\\misc\\moved.txt", L"GAME:\\misc\\two.txt", 0) == FALSE &&
                  GetLastError() == ERROR_ALREADY_EXISTS,
              "MoveFileExW will not replace a package file without MOVEFILE_REPLACE_EXISTING");
        check(move_ex_w(L"GAME:\\misc\\moved.txt", L"GAME:\\misc\\two.txt", MOVEFILE_REPLACE_EXISTING) != FALSE,
              "and replaces it with the flag");
        HANDLE h = create_w(L"GAME:\\misc\\two.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE && read_handle(h) == "one", "the replaced name reads the moved bytes");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
        check(read_path(package / "misc" / "two.txt") == "two", "the package's own two.txt is untouched");
        check(!std::filesystem::exists(overlay / "misc" / "moved.txt"), "the overlay source of the move is gone");
        check(move_a("GAME:\\misc\\three.txt", "GAME:\\cache\\three.txt") != FALSE &&
                  read_path(overlay / "cache" / "three.txt") == "three",
              "MoveFileA moves into another folder");
        check(move_w(L"GAME:\\cache\\from_template", L"GAME:\\cache\\renamed") != FALSE &&
                  std::filesystem::is_directory(overlay / "cache" / "renamed"),
              "an overlay folder is renamed");
        SetLastError(0);
        check(move_w(L"GAME:\\misc", L"GAME:\\misc2") == FALSE && GetLastError() == ERROR_ACCESS_DENIED,
              "a package folder is not moved");
        SetLastError(0);
        check(move_w(L"GAME:\\misc\\keep.txt", (root / "host.txt").c_str()) == FALSE &&
                  GetLastError() == ERROR_NOT_SAME_DEVICE,
              "a move between GAME: and a host path is refused as cross-volume");
        SetLastError(0);
        check(move_ex_w(L"GAME:\\misc\\keep.txt", nullptr, MOVEFILE_DELAY_UNTIL_REBOOT) == FALSE &&
                  GetLastError() == ERROR_INVALID_PARAMETER,
              "a boot-time delete of a console file is refused");
        SetLastError(0);
        check(move_w(L"GAME:\\misc\\gone.txt", L"GAME:\\misc\\x.txt") == FALSE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "moving a missing file reports ERROR_FILE_NOT_FOUND");
        SetLastError(0);
        check(move_w(L"GAME:\\misc\\keep.txt", L"GAME:\\nowhere\\keep.txt") == FALSE &&
                  GetLastError() == ERROR_PATH_NOT_FOUND,
              "moving into a missing folder reports ERROR_PATH_NOT_FOUND");
    }

    // MOVEFILE_WRITE_THROUGH on a package file: the copy and the marker are flushed before the call returns.
    {
        write_path(package / "misc" / "wt.txt", "wt");
        check(move_ex_w(L"GAME:\\misc\\wt.txt", L"GAME:\\misc\\wt_moved.txt", MOVEFILE_WRITE_THROUGH) != FALSE,
              "MoveFileExW with MOVEFILE_WRITE_THROUGH moves a package file");
        check(read_path(overlay / "misc" / "wt_moved.txt") == "wt", "its bytes are in the overlay");
        SetLastError(0);
        check(attributes_w(L"GAME:\\misc\\wt.txt") == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_FILE_NOT_FOUND,
              "and the old name is deleted");
    }

    // CopyFile: reads where a read would, writes the overlay; an existing name counts even if only the package has it.
    {
        check(copy_w(L"GAME:\\misc\\keep.txt", L"GAME:\\cache\\keep_copy.txt", TRUE) != FALSE &&
                  read_path(overlay / "cache" / "keep_copy.txt") == "keep",
              "CopyFileW copies a package file into the overlay");
        check(read_path(package / "misc" / "keep.txt") == "keep", "and the source stays");
        SetLastError(0);
        check(copy_w(L"GAME:\\misc\\keep.txt", L"GAME:\\data\\sub", FALSE) == FALSE && GetLastError() == ERROR_ACCESS_DENIED,
              "a copy onto a folder is refused");
        write_path(package / "misc" / "target.txt", "target");
        SetLastError(0);
        check(copy_w(L"GAME:\\misc\\keep.txt", L"GAME:\\misc\\target.txt", TRUE) == FALSE &&
                  GetLastError() == ERROR_FILE_EXISTS,
              "fail-if-exists sees a name only the package has");
        check(copy_a("GAME:\\misc\\keep.txt", "GAME:\\misc\\target.txt", FALSE) != FALSE &&
                  read_path(overlay / "misc" / "target.txt") == "keep" && read_path(package / "misc" / "target.txt") == "target",
              "CopyFileA overwrites through the overlay and leaves the package file alone");
        const std::filesystem::path host_copy = root / "host_copy.txt";
        check(copy_w(L"GAME:\\misc\\keep.txt", host_copy.c_str(), TRUE) != FALSE && read_path(host_copy) == "keep",
              "a copy from GAME: to a host path works");
        check(copy_ex_w(host_copy.c_str(), L"GAME:\\cache\\from_host.txt", nullptr, nullptr, nullptr, COPY_FILE_FAIL_IF_EXISTS) !=
                      FALSE &&
                  read_path(overlay / "cache" / "from_host.txt") == "keep",
              "CopyFileExW copies from a host path into GAME:");
        COPYFILE2_EXTENDED_PARAMETERS params{};
        params.dwSize = sizeof(params);
        params.dwCopyFlags = COPY_FILE_FAIL_IF_EXISTS;
        check(copy2(L"GAME:\\misc\\keep.txt", L"GAME:\\cache\\copy2.txt", &params) == S_OK &&
                  read_path(overlay / "cache" / "copy2.txt") == "keep",
              "CopyFile2 copies too");
        check(copy2(L"GAME:\\misc\\keep.txt", L"GAME:\\misc\\two.txt", &params) == HRESULT_FROM_WIN32(ERROR_FILE_EXISTS),
              "and returns its HRESULT for an existing name");
        SetLastError(0);
        check(copy_w(L"GAME:\\misc\\one.txt", L"GAME:\\cache\\one.txt", FALSE) == FALSE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "a deleted name cannot be copied");
    }

    // SetFileAttributes: the overlay copy gets the attributes; the package keeps its own.
    {
        const DWORD before = ::GetFileAttributesW((package / "misc" / "keep.txt").c_str());
        check(setattr_w(L"GAME:\\misc\\keep.txt", FILE_ATTRIBUTE_READONLY) != FALSE, "SetFileAttributesW on a package file");
        const DWORD now = attributes_w(L"GAME:\\misc\\keep.txt");
        check(now != INVALID_FILE_ATTRIBUTES && (now & FILE_ATTRIBUTE_READONLY) != 0, "the title sees the new attributes");
        check(read_path(overlay / "misc" / "keep.txt") == "keep", "on a copy of the file in the overlay");
        check(::GetFileAttributesW((package / "misc" / "keep.txt").c_str()) == before, "the package file keeps its own");
        check(setattr_a("GAME:\\misc\\keep.txt", FILE_ATTRIBUTE_NORMAL) != FALSE &&
                  (attributes_w(L"GAME:\\misc\\keep.txt") & FILE_ATTRIBUTE_READONLY) == 0,
              "SetFileAttributesA changes the overlay copy back");
        check(setattr_w(L"GAME:\\data\\sub", FILE_ATTRIBUTE_HIDDEN) != FALSE &&
                  (attributes_w(L"GAME:\\data\\sub") & FILE_ATTRIBUTE_HIDDEN) != 0,
              "a package folder's attributes are set on an overlay mirror");
        check(count(list(first_w, next_w, find_close, L"GAME:\\data\\*"), L"sub") == 1, "which is listed once");
        SetLastError(0);
        check(setattr_w(L"GAME:\\misc\\one.txt", FILE_ATTRIBUTE_NORMAL) == FALSE && GetLastError() == ERROR_FILE_NOT_FOUND,
              "a deleted name has no attributes to set");
    }

    // Nothing leaves the package and the overlay.
    {
        SetLastError(0);
        check(mkdir_w(L"GAME:\\..\\escape_dir", nullptr) == FALSE && GetLastError() == ERROR_INVALID_NAME &&
                  !std::filesystem::exists(root / "escape_dir"),
              "a folder above GAME: is refused");
        SetLastError(0);
        check(move_w(L"GAME:\\misc\\keep.txt", L"GAME:\\..\\escape.txt") == FALSE && GetLastError() == ERROR_INVALID_NAME,
              "a move above GAME: is refused");
        SetLastError(0);
        check(copy_w(L"GAME:\\misc\\keep.txt", L"GAME:\\C:\\x", FALSE) == FALSE && GetLastError() == ERROR_INVALID_NAME,
              "a copy to a drive inside GAME: is refused");
        SetLastError(0);
        check(rmdir_w(L"GAME:\\..") == FALSE && GetLastError() == ERROR_INVALID_NAME, "removing above GAME: is refused");
    }

    // Nothing leaves the package and the overlay.
    {
        SetLastError(0);
        const HANDLE h = create_w(L"GAME:\\..\\escape.txt", GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, 0, nullptr);
        check(h == INVALID_HANDLE_VALUE && GetLastError() == ERROR_INVALID_NAME, "a path above GAME: is refused");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
        check(!std::filesystem::exists(root / "escape.txt"), "and nothing was created outside");
        SetLastError(0);
        check(attributes_w(L"GAME:\\..\\x") == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_INVALID_NAME,
              "a stat above GAME: is refused");
        SetLastError(0);
        check(create_w(L"GAME:\\C:\\x", GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, 0, nullptr) == INVALID_HANDLE_VALUE &&
                  GetLastError() == ERROR_INVALID_NAME,
              "a drive inside GAME: is refused");
        SetLastError(0);
        WIN32_FIND_DATAW found{};
        check(first_w(L"GAME:\\..\\*", &found) == INVALID_HANDLE_VALUE && GetLastError() == ERROR_INVALID_NAME,
              "a listing above GAME: is refused");
        SetLastError(0);
        check(first_w(L"GAME:\\data\\*", nullptr) == INVALID_HANDLE_VALUE &&
                  GetLastError() == ERROR_INVALID_PARAMETER,
              "a null listing buffer is refused");
    }

    // CreateFile2 and FindFirstFileExA/W (used by GDK/OneCore titles and std::filesystem).
    {
        const auto create2 = resolve<CreateFile2Fn>("api-ms-win-core-file-l1-2-0.dll", "CreateFile2");
        const auto first_ex_w = resolve<FindFirstFileExWFn>("api-ms-win-core-file-l1-1-0.dll", "FindFirstFileExW");
        const auto first_ex_a = resolve<FindFirstFileExAFn>("kernelbase.dll", "FindFirstFileExA");
        check(create2 && first_ex_w && first_ex_a, "CreateFile2 and FindFirstFileExA/W are registered");

        // CreateFile2 reads an untouched package file without modifying it.
        write_path(package / "data" / "cf2_pkg.txt", "pkg-cf2");
        CREATEFILE2_EXTENDED_PARAMETERS ext{};
        ext.dwSize = sizeof(ext);
        ext.dwFileAttributes = FILE_ATTRIBUTE_NORMAL;
        const HANDLE h_read = create2(L"GAME:\\data\\cf2_pkg.txt", GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, &ext);
        check(h_read != INVALID_HANDLE_VALUE && read_handle(h_read) == "pkg-cf2",
              "CreateFile2 reads a GAME: file from the package");
        if (h_read != INVALID_HANDLE_VALUE) ::CloseHandle(h_read);

        // CreateFile2 write goes to the overlay, leaving the package untouched.
        const HANDLE h_write = create2(L"GAME:\\data\\cf2.txt", GENERIC_WRITE, 0, CREATE_ALWAYS, &ext);
        check(h_write != INVALID_HANDLE_VALUE, "CreateFile2 creates a new GAME: file in the overlay");
        if (h_write != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            ::WriteFile(h_write, "cf2", 3, &written, nullptr);
            ::CloseHandle(h_write);
        }
        check(read_path(overlay / "data" / "cf2.txt") == "cf2" && !std::filesystem::exists(package / "data" / "cf2.txt"),
              "CreateFile2 writes land in the overlay and never in the package");

        // CreateFile2 refuses path traversal.
        SetLastError(0);
        const HANDLE h_esc = create2(L"GAME:\\..\\cf2_escape.txt", GENERIC_WRITE, 0, CREATE_ALWAYS, &ext);
        check(h_esc == INVALID_HANDLE_VALUE && GetLastError() == ERROR_INVALID_NAME,
              "CreateFile2 refuses a path above GAME:");
        if (h_esc != INVALID_HANDLE_VALUE) ::CloseHandle(h_esc);

        // MAXIMUM_ALLOWED asks for every right the file grants, write included, without naming one. Such an open of
        // a package file is a write-open: it gets the overlay copy, so a write through the handle cannot reach the
        // package. CreateFileW, CreateFileA and CreateFile2 share the rule. The write is shorter than the file, so
        // the bytes after it show the overlay copy started as the package file's contents (OPEN_EXISTING keeps them).
        {
            write_path(package / "data" / "max_w.txt", "pkg-max");
            write_path(package / "data" / "max_a.txt", "pkg-max");
            write_path(package / "data" / "max_2.txt", "pkg-max");
            const auto write_through = [](HANDLE h) {
                if (h == INVALID_HANDLE_VALUE) return false;
                DWORD written = 0;
                const BOOL ok = ::WriteFile(h, "OV", 2, &written, nullptr);
                ::CloseHandle(h);
                return ok != FALSE && written == 2;
            };
            check(write_through(create_w(L"GAME:\\data\\max_w.txt", MAXIMUM_ALLOWED, 0, nullptr, OPEN_EXISTING, 0,
                                         nullptr)),
                  "CreateFileW(MAXIMUM_ALLOWED, OPEN_EXISTING) opens a package file for writing");
            check(read_path(package / "data" / "max_w.txt") == "pkg-max",
                  "a write through a MAXIMUM_ALLOWED handle does not reach the package (CreateFileW)");
            check(read_path(overlay / "data" / "max_w.txt") == "OVg-max",
                  "the write through a MAXIMUM_ALLOWED handle is in the overlay copy (CreateFileW)");

            check(write_through(create_a("GAME:\\data\\max_a.txt", MAXIMUM_ALLOWED | GENERIC_READ, 0, nullptr,
                                         OPEN_EXISTING, 0, nullptr)),
                  "CreateFileA(MAXIMUM_ALLOWED | GENERIC_READ, OPEN_EXISTING) opens a package file for writing");
            check(read_path(package / "data" / "max_a.txt") == "pkg-max" &&
                      read_path(overlay / "data" / "max_a.txt") == "OVg-max",
                  "the write goes to the overlay and not the package (CreateFileA)");

            check(write_through(create2(L"GAME:\\data\\max_2.txt", MAXIMUM_ALLOWED, 0, OPEN_EXISTING, &ext)),
                  "CreateFile2(MAXIMUM_ALLOWED, OPEN_EXISTING) opens a package file for writing");
            check(read_path(package / "data" / "max_2.txt") == "pkg-max" &&
                      read_path(overlay / "data" / "max_2.txt") == "OVg-max",
                  "the write goes to the overlay and not the package (CreateFile2)");

            // A plain read still opens the package file in place: no overlay copy appears for it.
            write_path(package / "data" / "max_read.txt", "pkg-max");
            const HANDLE h_plain = create_w(L"GAME:\\data\\max_read.txt", GENERIC_READ, FILE_SHARE_READ, nullptr,
                                            OPEN_EXISTING, 0, nullptr);
            check(h_plain != INVALID_HANDLE_VALUE && read_handle(h_plain) == "pkg-max" &&
                      !std::filesystem::exists(overlay / "data" / "max_read.txt"),
                  "a GENERIC_READ open is still a read: no overlay copy is made");
            if (h_plain != INVALID_HANDLE_VALUE) ::CloseHandle(h_plain);
        }

        // FindFirstFileExW merges package + overlay and honours FindExInfoBasic & FindExSearchLimitToDirectories.
        WIN32_FIND_DATAW fd{};
        std::vector<std::wstring> basic_names;
        bool alt_empty = true;
        HANDLE hf = first_ex_w(L"GAME:\\data\\*", FindExInfoBasic, &fd, FindExSearchNameMatch, nullptr, 0);
        check(hf != INVALID_HANDLE_VALUE, "FindFirstFileExW opens a GAME: directory listing");
        if (hf != INVALID_HANDLE_VALUE) {
            do {
                basic_names.emplace_back(fd.cFileName);
                if (fd.cAlternateFileName[0] != L'\0') alt_empty = false;
            } while (next_w(hf, &fd));
            find_close(hf);
        }
        check(contains(basic_names, L"hello.txt") && contains(basic_names, L"cf2.txt") && !contains(basic_names, L"bye.txt"),
              "FindFirstFileExW merges overlay + package and hides deleted files");
        check(alt_empty, "FindFirstFileExW(FindExInfoBasic) leaves cAlternateFileName empty");

        std::vector<std::wstring> dir_names;
        HANDLE hdir = first_ex_w(L"GAME:\\data\\*", FindExInfoStandard, &fd, FindExSearchLimitToDirectories, nullptr, 0);
        check(hdir != INVALID_HANDLE_VALUE, "FindFirstFileExW(FindExSearchLimitToDirectories) succeeds");
        if (hdir != INVALID_HANDLE_VALUE) {
            do {
                dir_names.emplace_back(fd.cFileName);
            } while (next_w(hdir, &fd));
            find_close(hdir);
        }
        check(contains(dir_names, L"sub") && !contains(dir_names, L"hello.txt"),
              "FindFirstFileExW(FindExSearchLimitToDirectories) returns only directories");

        // FindFirstFileExA works on GAME: paths too.
        WIN32_FIND_DATAA fda{};
        HANDLE hfa = first_ex_a("GAME:\\data\\cf2.txt", FindExInfoBasic, &fda, FindExSearchNameMatch, nullptr, 0);
        check(hfa != INVALID_HANDLE_VALUE && std::string(fda.cFileName) == "cf2.txt",
              "FindFirstFileExA finds an overlay file under GAME:");
        if (hfa != INVALID_HANDLE_VALUE) find_close(hfa);
    }

    // Copy-up under two threads: both write-open the same untouched package file at once. Each open must succeed and
    // the overlay copy must hold the package's bytes; the copy goes through a private name, so neither thread can find
    // the name taken mid-copy or read a half-written file.
    {
        const std::string big(256 * 1024, 'p');  // long enough that the two copies overlap
        bool all_opened = true, all_intact = true;
        for (int round = 0; round < 50; ++round) {
            const std::wstring name = L"race" + std::to_wstring(round) + L".bin";
            write_path(package / "data" / name, big);
            const std::wstring game = L"GAME:\\data\\" + name;
            HANDLE handles[2] = {INVALID_HANDLE_VALUE, INVALID_HANDLE_VALUE};
            std::thread threads[2];
            for (int i = 0; i < 2; ++i)
                threads[i] = std::thread([&, i] {
                    handles[i] = create_w(game.c_str(), GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                          nullptr, OPEN_EXISTING, 0, nullptr);                });
            for (auto& t : threads) t.join();
            for (HANDLE h : handles) {
                if (h == INVALID_HANDLE_VALUE) all_opened = false;
                else ::CloseHandle(h);
            }
            if (read_path(overlay / "data" / name) != big) all_intact = false;
        }
        check(all_opened, "two threads write-opening one package file both succeed");
        check(all_intact, "the overlay copy made under two threads holds the whole package file");
        std::error_code ec;
        check(std::filesystem::is_empty(overlay / ".gta6rt-copy", ec) && !ec, "copy-up leaves no staging file behind");

        // A leftover from a process killed mid-copy stays out of the title's view.
        write_path(overlay / ".gta6rt-copy" / "123-1", "half");
        check(!contains(list(first_w, next_w, find_close, L"GAME:\\*"), L".gta6rt-copy"),
              "the copy staging folder is hidden from a GAME: listing");
    }

    // RT-218: "G:\" is the package and "T:\" a writable scratch folder in the overlay, as in the other console layers.
    // A host path under the configured folders stays a host path, and a path that climbs out of the drive is refused.
    {
        write_path(package / "drives" / "sub" / "probe.txt", "probe");
        HANDLE h = create_w(L"G:\\drives\\sub\\probe.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE && read_handle(h) == "probe", "CreateFileW opens G:\\drives\\sub\\probe.txt from the package");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
        h = create_w(L"g:/drives/sub/probe.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "the drive letter is case-insensitive and accepts forward slashes");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
        check(attributes_w(L"G:\\data\\sub") != INVALID_FILE_ATTRIBUTES, "GetFileAttributesW stats a G: folder");

        SetLastError(0);
        check(create_w(L"G:\\..\\x.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr) ==
                      INVALID_HANDLE_VALUE &&
                  GetLastError() == ERROR_INVALID_NAME,
              "G:\\.. is refused with ERROR_INVALID_NAME");

        h = create_w(L"T:\\cache\\scratch.bin", GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE, "CreateFileW creates a file on T: (parent folders and all)");
        if (h != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            ::WriteFile(h, "scratch", 7, &written, nullptr);
            ::CloseHandle(h);
        }
        check(read_path(overlay / ".gta6rt-temp" / "cache" / "scratch.bin") == "scratch",
              "a T: file lands in the overlay's scratch folder");
        check(!std::filesystem::exists(package / "cache" / "scratch.bin") && !std::filesystem::exists(package / ".gta6rt-temp"),
              "and the package is untouched");
        h = create_w(L"t:/cache/scratch.bin", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        check(h != INVALID_HANDLE_VALUE && read_handle(h) == "scratch", "the T: file reads back");
        if (h != INVALID_HANDLE_VALUE) ::CloseHandle(h);
        check(attributes_w(L"GAME:\\cache\\scratch.bin") == INVALID_FILE_ATTRIBUTES,
              "T: is its own drive: GAME: does not see the scratch file");
        check(!contains(list(first_w, next_w, find_close, L"GAME:\\*"), L".gta6rt-temp"),
              "a listing of GAME:\\ does not show the scratch folder");
        SetLastError(0);
        check(create_w(L"T:\\..\\data\\hello.txt", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr) ==
                      INVALID_HANDLE_VALUE &&
                  GetLastError() == ERROR_INVALID_NAME,
              "T:\\.. cannot climb out of the scratch folder into the package");

        // A package that itself sits on drive G: is reached by host paths, which must stay host paths.
        rt::fs::configure(L"G:\\leonida_missing_package", L"G:\\leonida_missing_overlay");
        SetLastError(0);
        check(attributes_w(L"G:\\leonida_missing_package\\data.txt") == INVALID_FILE_ATTRIBUTES &&
                  GetLastError() != ERROR_INVALID_NAME,
              "a path under a configured G: game folder is a host path");
        // No roots configured: nothing to map G: and T: onto, so they are host paths.
        rt::fs::configure({}, {});
        SetLastError(0);
        check(attributes_w(L"T:\\x") == INVALID_FILE_ATTRIBUTES && GetLastError() != ERROR_INVALID_NAME,
              "without configured folders G: and T: stay host drives");
        rt::fs::configure(package, overlay);
    }

    std::filesystem::remove_all(root);
    std::printf("%d check(s), %d failure(s)\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
