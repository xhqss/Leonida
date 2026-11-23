// Synthetic startup fixture: process-attach callbacks must see the entry point's command line.
#include <windows.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>

namespace {

struct Snapshot {
    int argc;
    char argv[8][256];
    wchar_t wargv[8][256];
    char line[2048];
    wchar_t wline[2048];
};

Snapshot g_startup{};
bool g_captured = false;

template <typename C, size_t N>
bool copy(C (&to)[N], const C* from) {
    if (!from) return false;
    size_t i = 0;
    for (; i + 1 < N && from[i]; ++i) to[i] = from[i];
    to[i] = 0;
    return from[i] == 0;
}

bool capture(Snapshot& s) {
    s.argc = *__p___argc();
    char** args = *__p___argv();
    wchar_t** wargs = *__p___wargv();
    if (s.argc < 1 || s.argc > 8 || !args || !wargs) return false;
    for (int i = 0; i < s.argc; ++i)
        if (!copy(s.argv[i], args[i]) || !copy(s.wargv[i], wargs[i])) return false;
    return !args[s.argc] && !wargs[s.argc] && copy(s.line, GetCommandLineA()) &&
           copy(s.wline, GetCommandLineW());
}

bool matches(const Snapshot& a, const Snapshot& b) {
    if (a.argc != b.argc || std::strcmp(a.line, b.line) || std::wcscmp(a.wline, b.wline)) return false;
    for (int i = 0; i < a.argc; ++i)
        if (std::strcmp(a.argv[i], b.argv[i]) || std::wcscmp(a.wargv[i], b.wargv[i])) return false;
    return true;
}

}  // namespace

#ifdef STARTUP_CMDLINE_DLL

extern "C" __declspec(dllexport) bool __cdecl startup_cmdline_matches(const Snapshot* entry) {
    return g_captured && matches(g_startup, *entry);
}

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_captured = capture(g_startup);
        if (std::strstr(GetCommandLineA(), "fail-init")) return FALSE;
    }
    return TRUE;
}

#else

extern "C" __declspec(dllimport) bool __cdecl startup_cmdline_matches(const Snapshot* entry);

namespace {
void NTAPI startup_callback(PVOID, DWORD reason, PVOID) {
    if (reason == DLL_PROCESS_ATTACH) g_captured = capture(g_startup);
}
}  // namespace

#pragma section(".CRT$XLB", read)
extern "C" __declspec(allocate(".CRT$XLB")) PIMAGE_TLS_CALLBACK g_startup_callback = startup_callback;
#pragma comment(linker, "/INCLUDE:_tls_used")
#pragma comment(linker, "/INCLUDE:g_startup_callback")

int main(int argc, char** argv) {
    Snapshot entry{};
    bool ok = capture(entry) && g_captured && matches(g_startup, entry) && startup_cmdline_matches(&entry);
    ok = ok && argc == entry.argc;
    for (int i = 0; ok && i < argc; ++i) ok = std::strcmp(argv[i], entry.argv[i]) == 0;
    std::printf("tiny_startup_cmdline: %s\n", ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

#endif
