// Checks the offline WinHTTP the shims give a mapped title (runtime/shims/winhttp_offline.cpp).
//
// Every function is resolved through rt::shims::find under winhttp.dll, so the test runs against the table the
// runtime installs. Nothing here touches the network: the server names use the reserved .invalid top-level domain, and
// the only host WinHTTP call the shims make is WinHttpCrackUrl, which parses a string.
#include <windows.h>
#include <winhttp.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <vector>

#include "shims.h"

namespace {

using OpenFn = HINTERNET(WINAPI*)(LPCWSTR, DWORD, LPCWSTR, LPCWSTR, DWORD);
using ConnectFn = HINTERNET(WINAPI*)(HINTERNET, LPCWSTR, INTERNET_PORT, DWORD);
using OpenRequestFn = HINTERNET(WINAPI*)(HINTERNET, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR*, DWORD);
using SendRequestFn = BOOL(WINAPI*)(HINTERNET, LPCWSTR, DWORD, LPVOID, DWORD, DWORD, DWORD_PTR);
using ReceiveResponseFn = BOOL(WINAPI*)(HINTERNET, LPVOID);
using ReadDataFn = BOOL(WINAPI*)(HINTERNET, LPVOID, DWORD, LPDWORD);
using QueryHeadersFn = BOOL(WINAPI*)(HINTERNET, DWORD, LPCWSTR, LPVOID, LPDWORD, LPDWORD);
using AddRequestHeadersFn = BOOL(WINAPI*)(HINTERNET, LPCWSTR, DWORD, DWORD);
using SetTimeoutsFn = BOOL(WINAPI*)(HINTERNET, int, int, int, int);
using SetOptionFn = BOOL(WINAPI*)(HINTERNET, DWORD, LPVOID, DWORD);
using QueryOptionFn = BOOL(WINAPI*)(HINTERNET, DWORD, LPVOID, LPDWORD);
using SetStatusCallbackFn = WINHTTP_STATUS_CALLBACK(WINAPI*)(HINTERNET, WINHTTP_STATUS_CALLBACK, DWORD, DWORD_PTR);
using CloseHandleFn = BOOL(WINAPI*)(HINTERNET);
using CrackUrlFn = BOOL(WINAPI*)(LPCWSTR, DWORD, DWORD, LPURL_COMPONENTS);

int g_checks = 0;
int g_failures = 0;

void check(bool condition, const char* what) {
    ++g_checks;
    std::printf("%s %s\n", condition ? "PASS" : "FAIL", what);
    if (!condition) ++g_failures;
}

template <typename Fn>
Fn resolve(const char* name) {
    return reinterpret_cast<Fn>(rt::shims::find("winhttp.dll", name));
}

// What the status callback saw, for the asynchronous checks.
struct Seen {
    std::atomic<int> request_errors{0};
    std::atomic<int> closing{0};
    std::atomic<int> created{0};
    std::atomic<DWORD> api{0};
    std::atomic<DWORD> error{0};
    std::atomic<DWORD_PTR> context{0};
    std::atomic<DWORD> thread{0};
    std::atomic<bool> closing_after_error{true};
    HANDLE error_event = nullptr;
    HANDLE closing_event = nullptr;
    CloseHandleFn close_in_callback = nullptr;  // when set, the error callback closes the request itself
};
Seen* g_seen = nullptr;

void CALLBACK on_status(HINTERNET handle, DWORD_PTR context, DWORD status, LPVOID info, DWORD length) {
    Seen& s = *g_seen;
    if (status == WINHTTP_CALLBACK_STATUS_HANDLE_CREATED) {
        ++s.created;
    } else if (status == WINHTTP_CALLBACK_STATUS_REQUEST_ERROR && info && length == sizeof(WINHTTP_ASYNC_RESULT)) {
        const auto* result = static_cast<const WINHTTP_ASYNC_RESULT*>(info);
        s.api = static_cast<DWORD>(result->dwResult);
        s.error = result->dwError;
        s.context = context;
        s.thread = GetCurrentThreadId();
        ++s.request_errors;
        if (s.close_in_callback) s.close_in_callback(handle);
        SetEvent(s.error_event);
    } else if (status == WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING) {
        if (s.request_errors.load() == 0) s.closing_after_error = false;
        if (info && length == sizeof(HINTERNET) && *static_cast<HINTERNET*>(info) == handle) ++s.closing;
        SetEvent(s.closing_event);
    }
}

double ms_since(std::chrono::steady_clock::time_point start) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
}

// HANDLE_CLOSING arrives on the callback thread, so a count read right after WinHttpCloseHandle can be short, and a
// notification that comes late lands in the next block's count (TEST-721). Waits up to two seconds for `n`.
bool wait_for_closing(const Seen& s, int n) {
    for (int i = 0; i < 200 && s.closing.load() < n; ++i) Sleep(10);
    return s.closing.load() >= n;
}

}  // namespace

int main() {
    const auto open = resolve<OpenFn>("WinHttpOpen");
    const auto connect = resolve<ConnectFn>("WinHttpConnect");
    const auto open_request = resolve<OpenRequestFn>("WinHttpOpenRequest");
    const auto send = resolve<SendRequestFn>("WinHttpSendRequest");
    const auto receive = resolve<ReceiveResponseFn>("WinHttpReceiveResponse");
    const auto read = resolve<ReadDataFn>("WinHttpReadData");
    const auto query_headers = resolve<QueryHeadersFn>("WinHttpQueryHeaders");
    const auto add_headers = resolve<AddRequestHeadersFn>("WinHttpAddRequestHeaders");
    const auto set_timeouts = resolve<SetTimeoutsFn>("WinHttpSetTimeouts");
    const auto set_option = resolve<SetOptionFn>("WinHttpSetOption");
    const auto query_option = resolve<QueryOptionFn>("WinHttpQueryOption");
    const auto set_callback = resolve<SetStatusCallbackFn>("WinHttpSetStatusCallback");
    const auto close = resolve<CloseHandleFn>("WinHttpCloseHandle");
    const auto crack = resolve<CrackUrlFn>("WinHttpCrackUrl");
    check(open && connect && open_request && send && receive && read && query_headers && add_headers &&
              set_timeouts && set_option && query_option && set_callback && close && crack &&
              rt::shims::find("winhttp.dll", "WinHttpQueryDataAvailable") &&
              rt::shims::find("winhttp.dll", "WinHttpWriteData"),
          "all 16 WinHTTP imports of the package resolve through the registry");
    check(rt::shims::owns_dll("winhttp.dll"), "winhttp.dll is a module the shims own (LoadLibrary gets a pseudo-handle)");
    if (!(open && connect && open_request && send && close && set_callback)) {
        std::printf("%d check(s), %d failure(s)\n", g_checks, g_failures + 1);
        return 1;
    }

    // Synchronous: the send fails at once, nothing is resolved or connected.
    {
        const HINTERNET session = open(L"test", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, 0);
        check(session != nullptr, "WinHttpOpen returns a session");
        const HINTERNET connection = connect(session, L"example.invalid", INTERNET_DEFAULT_HTTPS_PORT, 0);
        check(connection != nullptr, "WinHttpConnect returns a connection without resolving the name");
        const HINTERNET request =
            open_request(connection, L"GET", L"/x", nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                         WINHTTP_FLAG_SECURE);
        check(request != nullptr, "WinHttpOpenRequest returns a request");
        check(add_headers(request, L"X-Test: 1", static_cast<DWORD>(-1), WINHTTP_ADDREQ_FLAG_ADD) != FALSE,
              "request headers are accepted");
        check(set_timeouts(request, 1000, 1000, 1000, 1000) != FALSE, "timeouts are accepted");

        // The same answers the host's winhttp.dll gives (measured on Windows 11 26300).
        const auto rejects = [](BOOL ok) { return ok == FALSE && GetLastError() == ERROR_INVALID_PARAMETER; };
        SetLastError(0);
        check(rejects(add_headers(request, nullptr, 0, WINHTTP_ADDREQ_FLAG_ADD)), "a null header block is rejected");
        SetLastError(0);
        check(rejects(add_headers(request, L"", static_cast<DWORD>(-1), WINHTTP_ADDREQ_FLAG_ADD)),
              "an empty header block is rejected");
        SetLastError(0);
        check(rejects(add_headers(request, L"nocolon", static_cast<DWORD>(-1), WINHTTP_ADDREQ_FLAG_ADD)),
              "a header with no colon is rejected");
        SetLastError(0);
        check(rejects(add_headers(request, L"X-C: b", 2, WINHTTP_ADDREQ_FLAG_ADD)),
              "a length that cuts the header before its colon is rejected");
        check(add_headers(request, L"X-D: b", 6, WINHTTP_ADDREQ_FLAG_ADD) != FALSE,
              "an explicit length that covers the colon is accepted");
        check(set_timeouts(request, 0, 0, 0, 0) != FALSE && set_timeouts(request, -1, -1, -1, -1) != FALSE,
              "timeouts of 0 and -1 (infinite) are accepted");
        for (int field = 0; field < 4; ++field) {
            int t[4] = {0, 0, 0, 0};
            t[field] = -2;
            SetLastError(0);
            check(rejects(set_timeouts(request, t[0], t[1], t[2], t[3])), "a timeout below -1 is rejected");
        }

        SetLastError(0);
        const auto start = std::chrono::steady_clock::now();
        const BOOL sent = send(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0);
        const DWORD error = GetLastError();
        const double elapsed = ms_since(start);
        check(sent == FALSE && error == ERROR_WINHTTP_CANNOT_CONNECT,
              "WinHttpSendRequest fails with ERROR_WINHTTP_CANNOT_CONNECT");
        check(elapsed < 50.0, "and fails at once (under 50 ms)");
        SetLastError(0);
        check(receive(request, nullptr) == FALSE && GetLastError() == ERROR_WINHTTP_INCORRECT_HANDLE_STATE,
              "there is no response to receive");
        char buffer[16];
        DWORD got = 0;
        SetLastError(0);
        check(read(request, buffer, sizeof(buffer), &got) == FALSE &&
                  GetLastError() == ERROR_WINHTTP_INCORRECT_HANDLE_STATE,
              "and nothing to read");
        DWORD status = 0, size = sizeof(status);
        SetLastError(0);
        check(query_headers(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX,
                            &status, &size, WINHTTP_NO_HEADER_INDEX) == FALSE &&
                  GetLastError() == ERROR_WINHTTP_INCORRECT_HANDLE_STATE,
              "and no status code");

        // Options: the context value round-trips, the handle type is answered, the rest is refused when queried.
        DWORD_PTR value = 0x1234;
        check(set_option(request, WINHTTP_OPTION_CONTEXT_VALUE, &value, sizeof(value)) != FALSE,
              "WINHTTP_OPTION_CONTEXT_VALUE can be set");
        DWORD_PTR back = 0;
        DWORD back_size = sizeof(back);
        check(query_option(request, WINHTTP_OPTION_CONTEXT_VALUE, &back, &back_size) != FALSE && back == 0x1234,
              "and queried back");
        DWORD type = 0, type_size = sizeof(type);
        check(query_option(connection, WINHTTP_OPTION_HANDLE_TYPE, &type, &type_size) != FALSE &&
                  type == WINHTTP_HANDLE_TYPE_CONNECT,
              "WINHTTP_OPTION_HANDLE_TYPE names a connection");
        DWORD needed = 0;
        SetLastError(0);
        check(query_option(request, WINHTTP_OPTION_CONTEXT_VALUE, nullptr, &needed) == FALSE &&
                  GetLastError() == ERROR_INSUFFICIENT_BUFFER && needed == sizeof(DWORD_PTR),
              "a query without a buffer reports the size it needs");
        DWORD flags = 0, flags_size = sizeof(flags);
        SetLastError(0);
        check(query_option(request, WINHTTP_OPTION_SECURITY_FLAGS, &flags, &flags_size) == FALSE &&
                  GetLastError() == ERROR_WINHTTP_INVALID_OPTION,
              "other options are not answered");
        DWORD protocols = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2;
        check(set_option(session, WINHTTP_OPTION_SECURE_PROTOCOLS, &protocols, sizeof(protocols)) != FALSE,
              "but setting them is accepted");

        // Wrong handles.
        SetLastError(0);
        check(connect(request, L"example.invalid", 80, 0) == nullptr &&
                  GetLastError() == ERROR_WINHTTP_INCORRECT_HANDLE_TYPE,
              "a request is not a session");
        SetLastError(0);
        check(send(connection, nullptr, 0, nullptr, 0, 0, 0) == FALSE &&
                  GetLastError() == ERROR_WINHTTP_INCORRECT_HANDLE_TYPE,
              "a connection is not a request");
        SetLastError(0);
        check(connect(session, nullptr, 80, 0) == nullptr && GetLastError() == ERROR_INVALID_PARAMETER,
              "a connection needs a server name");

        check(close(request) != FALSE && close(connection) != FALSE && close(session) != FALSE,
              "the handles close, children first");
        SetLastError(0);
        check(close(request) == FALSE && GetLastError() == ERROR_INVALID_HANDLE, "a closed handle is gone");
        SetLastError(0);
        check(send(request, nullptr, 0, nullptr, 0, 0, 0) == FALSE && GetLastError() == ERROR_INVALID_HANDLE,
              "and cannot be used");
    }

    // Asynchronous: the send returns TRUE, the failure arrives through the inherited callback on another thread with
    // the request's context, and HANDLE_CLOSING comes last.
    Seen seen;
    seen.error_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    seen.closing_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    g_seen = &seen;
    {
        const HINTERNET session = open(L"test", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, WINHTTP_FLAG_ASYNC);
        check(set_callback(session, &on_status, WINHTTP_CALLBACK_FLAG_ALL_NOTIFICATIONS, 0) == nullptr,
              "WinHttpSetStatusCallback returns the previous callback (none)");
        const HINTERNET connection = connect(session, L"example.invalid", 443, 0);
        const HINTERNET request = open_request(connection, L"POST", L"/y", nullptr, WINHTTP_NO_REFERER,
                                               WINHTTP_DEFAULT_ACCEPT_TYPES, WINHTTP_FLAG_SECURE);
        check(connection && request && seen.created.load() == 2,
              "the callback is inherited and reports both new handles as created");

        const auto start = std::chrono::steady_clock::now();
        const BOOL sent = send(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0xC0FFEE);
        check(sent != FALSE, "an asynchronous WinHttpSendRequest returns TRUE");
        const bool fired = WaitForSingleObject(seen.error_event, 2000) == WAIT_OBJECT_0;
        const double elapsed = ms_since(start);
        check(fired && seen.request_errors.load() == 1, "WINHTTP_CALLBACK_STATUS_REQUEST_ERROR arrives once");
        check(seen.api.load() == API_SEND_REQUEST && seen.error.load() == ERROR_WINHTTP_CANNOT_CONNECT,
              "for API_SEND_REQUEST with ERROR_WINHTTP_CANNOT_CONNECT");
        check(seen.context.load() == 0xC0FFEE, "with the context the request was sent with");
        check(seen.thread.load() != GetCurrentThreadId(), "on another thread");
        check(elapsed < 500.0, "within 500 ms");

        check(close(request) != FALSE, "the request closes");
        check(WaitForSingleObject(seen.closing_event, 2000) == WAIT_OBJECT_0 && seen.closing.load() == 1 &&
                  seen.closing_after_error.load(),
              "and HANDLE_CLOSING follows once, after the error");
        close(connection);
        close(session);
        check(wait_for_closing(seen, 3) && seen.closing.load() == 3, "every handle reports HANDLE_CLOSING once");
    }

    // A callback that closes its request while handling the error, as libHttpClient-style code does.
    {
        seen.request_errors = 0;
        seen.closing = 0;
        seen.close_in_callback = close;
        ResetEvent(seen.closing_event);
        const HINTERNET session = open(L"test", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, WINHTTP_FLAG_ASYNC);
        set_callback(session, &on_status, WINHTTP_CALLBACK_FLAG_ALL_NOTIFICATIONS, 0);
        const HINTERNET connection = connect(session, L"example.invalid", 443, 0);
        const HINTERNET request = open_request(connection, L"GET", L"/z", nullptr, WINHTTP_NO_REFERER,
                                               WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
        check(send(request, nullptr, 0, nullptr, 0, 0, 7) != FALSE, "a second asynchronous send is queued");
        check(WaitForSingleObject(seen.error_event, 2000) == WAIT_OBJECT_0, "its error arrives");
        check(WaitForSingleObject(seen.closing_event, 2000) == WAIT_OBJECT_0 && seen.closing.load() == 1,
              "closing the request inside the callback reports HANDLE_CLOSING once");
        seen.close_in_callback = nullptr;
        close(connection);
        close(session);
        check(wait_for_closing(seen, 3) && seen.closing.load() == 3,
              "the connection and the session report HANDLE_CLOSING before the next block counts its own");
    }

    // A request closed before its queued failure is delivered gets no error callback, only HANDLE_CLOSING.
    {
        seen.request_errors = 0;
        seen.closing = 0;
        const HINTERNET session = open(L"test", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, WINHTTP_FLAG_ASYNC);
        set_callback(session, &on_status, WINHTTP_CALLBACK_FLAG_HANDLES, 0);  // closing only
        const HINTERNET connection = connect(session, L"example.invalid", 443, 0);
        const HINTERNET request = open_request(connection, L"GET", L"/w", nullptr, WINHTTP_NO_REFERER,
                                               WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
        send(request, nullptr, 0, nullptr, 0, 0, 0);
        close(request);
        close(connection);
        close(session);
        for (int i = 0; i < 100 && seen.closing.load() < 3; ++i) Sleep(10);
        check(seen.request_errors.load() == 0 && seen.closing.load() == 3,
              "notifications not asked for are not sent, and each handle still closes once");
    }
    g_seen = nullptr;
    CloseHandle(seen.error_event);
    CloseHandle(seen.closing_event);

    // WINHTTP_OPTION_CONTEXT_VALUE is inherited like the callback: a handle starts with the context its parent has
    // when it is created, and keeps it when the parent's changes later. The host's winhttp.dll gave these values.
    {
        struct Event {
            HINTERNET handle;
            DWORD status;
            DWORD_PTR context;
        };
        static std::vector<Event> events;  // a synchronous session: every callback runs on this thread
        const WINHTTP_STATUS_CALLBACK record = [](HINTERNET handle, DWORD_PTR context, DWORD status, LPVOID, DWORD) {
            events.push_back({handle, status, context});
        };
        const auto context_of = [&](HINTERNET handle) -> DWORD_PTR {
            DWORD_PTR value = ~DWORD_PTR{0};
            DWORD size = sizeof(value);
            return query_option(handle, WINHTTP_OPTION_CONTEXT_VALUE, &value, &size) ? value : ~DWORD_PTR{0};
        };
        const auto set_context = [&](HINTERNET handle, DWORD_PTR value) {
            return set_option(handle, WINHTTP_OPTION_CONTEXT_VALUE, &value, sizeof(value)) != FALSE;
        };
        const auto context_at = [&](HINTERNET handle, DWORD status) -> DWORD_PTR {
            for (const Event& e : events)
                if (e.handle == handle && e.status == status) return e.context;
            return ~DWORD_PTR{0};
        };

        const HINTERNET session = open(L"test", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, 0);
        set_callback(session, record, WINHTTP_CALLBACK_FLAG_HANDLES, 0);
        check(set_context(session, 7), "WINHTTP_OPTION_CONTEXT_VALUE can be set on a session");
        const HINTERNET connection = connect(session, L"127.0.0.1", 1, 0);
        const HINTERNET request = open_request(connection, L"GET", L"/", nullptr, WINHTTP_NO_REFERER,
                                               WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
        check(context_of(connection) == 7 && context_of(request) == 7,
              "a connection and a request start with the session's context");
        check(context_at(connection, WINHTTP_CALLBACK_STATUS_HANDLE_CREATED) == 7 &&
                  context_at(request, WINHTTP_CALLBACK_STATUS_HANDLE_CREATED) == 7,
              "and their HANDLE_CREATED callbacks carry it");

        check(set_context(request, 9) && context_of(request) == 9 && context_of(connection) == 7,
              "a context set on the request replaces the inherited one for that request only");
        check(set_context(session, 11) && context_of(connection) == 7 && context_of(request) == 9,
              "a context set on the session later does not change handles that exist");
        const HINTERNET late = connect(session, L"127.0.0.1", 1, 0);
        check(context_of(late) == 11, "a connection made after that starts with the new one");

        const HINTERNET sent = open_request(connection, L"GET", L"/", nullptr, WINHTTP_NO_REFERER,
                                            WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
        check(context_of(sent) == 7, "a request starts with its connection's context, not the session's");
        send(sent, WINHTTP_NO_ADDITIONAL_HEADERS, 0, WINHTTP_NO_REQUEST_DATA, 0, 0, 0x51);
        check(context_of(sent) == 0x51, "the context WinHttpSendRequest is given replaces the inherited one");

        close(sent);
        close(request);
        close(late);
        close(connection);
        close(session);
        check(context_at(sent, WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING) == 0x51 &&
                  context_at(request, WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING) == 9 &&
                  context_at(late, WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING) == 11 &&
                  context_at(connection, WINHTTP_CALLBACK_STATUS_HANDLE_CLOSING) == 7,
              "HANDLE_CLOSING carries each handle's own context");
    }

    // WinHttpCrackUrl is parsing only and stays the host's.
    {
        wchar_t host[64] = {}, path[64] = {};
        URL_COMPONENTS parts{};
        parts.dwStructSize = sizeof(parts);
        parts.lpszHostName = host;
        parts.dwHostNameLength = 64;
        parts.lpszUrlPath = path;
        parts.dwUrlPathLength = 64;
        check(crack(L"https://example.invalid:8443/a/b", 0, 0, &parts) != FALSE && parts.nPort == 8443 &&
                  parts.nScheme == INTERNET_SCHEME_HTTPS && std::wcscmp(host, L"example.invalid") == 0 &&
                  std::wcscmp(path, L"/a/b") == 0,
              "WinHttpCrackUrl parses a URL");
    }

    std::printf("%d check(s), %d failure(s)\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
