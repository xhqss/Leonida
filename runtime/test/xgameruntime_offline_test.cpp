// XGameEventImpl, XGameUiImpl and XGameStreamingImpl through QueryApiImpl, called the way xgameruntime.lib's thunks
// call them: QueryApiImpl with the class id (or an interface id), one vtable slot with `this` first. Slots follow
// xgameruntime-docs' layout; arguments follow the public GDK reference. No game data; nothing online.
#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include <XAsync.h>
#include <XAsyncProvider.h>
#include <XTaskQueue.h>

#include "shims.h"

namespace rt::shims {
long xgameruntime_offline_game_events();
long xgameruntime_user_open_handles();
}

namespace {

int g_checks = 0;
int g_failures = 0;

void check(bool condition, const char* what) {
    ++g_checks;
    std::printf("%s %s\n", condition ? "PASS" : "FAIL", what);
    if (!condition) ++g_failures;
}

constexpr GUID kEventClass = {0xbbfbdcc7, 0xbfe7, 0x409b, {0xa5, 0xca, 0xed, 0xf0, 0x54, 0x96, 0x0b, 0x4d}};
constexpr GUID kUiClass = {0xdfcd4649, 0x4ff8, 0x4043, {0xba, 0x07, 0x35, 0xd6, 0x07, 0xdf, 0x98, 0xb0}};
constexpr GUID kUiImpl = {0x6eeaa73e, 0x9669, 0x43ad, {0xa2, 0xc7, 0xd0, 0xda, 0x4e, 0x1f, 0x50, 0xa1}};
constexpr GUID kUiImpl4 = {0xeaf669df, 0x5542, 0x4590, {0x99, 0xa3, 0x8d, 0xc0, 0x61, 0xf8, 0x37, 0xcc}};
constexpr GUID kStreamingClass = {0x0a2192aa, 0xb2d5, 0x4d58, {0x83, 0xbe, 0x38, 0x3b, 0x6d, 0x80, 0x79, 0x9e}};
constexpr GUID kStreamingImpl3 = {0x57786622, 0x6605, 0x46d0, {0xb9, 0x17, 0x0f, 0x22, 0xbb, 0xcd, 0x9c, 0x52}};

using Query = HRESULT(WINAPI*)(const GUID*, REFIID, void**);
using Fn = void*;
Fn slot(void* obj, int index) { return (*static_cast<Fn* const*>(obj))[index]; }
template <class F> F at(void* obj, int index) { return reinterpret_cast<F>(slot(obj, index)); }

bool finished(XAsyncBlock* async) { return XAsyncGetStatus(async, true) != E_PENDING; }

using EventWrite = HRESULT(STDMETHODCALLTYPE*)(void*, void*, const char*, const char*, const char*, const char*,
                                               const char*);
using Dialog = HRESULT(STDMETHODCALLTYPE*)(void*, XAsyncBlock*, const char*, const char*, const char*, const char*,
                                           const char*, uint32_t, uint32_t);
using DialogResult = HRESULT(STDMETHODCALLTYPE*)(void*, XAsyncBlock*, uint32_t*);
using ErrorDialog = HRESULT(STDMETHODCALLTYPE*)(void*, XAsyncBlock*, HRESULT, const char*);
using AsyncOnly = HRESULT(STDMETHODCALLTYPE*)(void*, XAsyncBlock*);
using NoArgs = HRESULT(STDMETHODCALLTYPE*)(void*);
using Bool = bool(STDMETHODCALLTYPE*)(void*);
using Count = uint32_t(STDMETHODCALLTYPE*)(void*);
using Register = HRESULT(STDMETHODCALLTYPE*)(void*, XTaskQueueHandle, void*, void*, XTaskQueueRegistrationToken*);

using TryHdr = uint32_t(STDMETHODCALLTYPE*)(void*, uint32_t, float*);
using FindUser = HRESULT(STDMETHODCALLTYPE*)(void*, const uint8_t*, void**);
using CloseUser = void(STDMETHODCALLTYPE*)(void*, void*);
using Unregister = bool(STDMETHODCALLTYPE*)(void*, XTaskQueueRegistrationToken, bool);
using Endpoint = HRESULT(STDMETHODCALLTYPE*)(void*, uint64_t, uint32_t, size_t, wchar_t*, size_t*);
using FindController = HRESULT(STDMETHODCALLTYPE*)(void*, void*, XAsyncBlock*);
using FindControllerResult = HRESULT(STDMETHODCALLTYPE*)(void*, XAsyncBlock*, uint8_t*);

constexpr GUID kDisplayClass = {0x03f0fe74, 0xfdd9, 0x4e5c, {0xb6, 0x30, 0xf9, 0x33, 0x9c, 0x47, 0xac, 0xc5}};
constexpr GUID kUserDeviceClass = {0x7d824997, 0x10dc, 0x45ab, {0x86, 0xb7, 0x27, 0x37, 0x76, 0x7c, 0x0b, 0xf1}};
constexpr GUID kUserClass = {0x01acd177, 0x91f9, 0x4763, {0xa3, 0x8e, 0xcc, 0xbb, 0x55, 0xce, 0x32, 0xe0}};

void* user_object(HRESULT(WINAPI* query)(const GUID*, REFIID, void**)) {
    void* user = nullptr;
    query(&kUserClass, kUserClass, &user);
    return user;
}

void CALLBACK on_state(void*, uint64_t, uint32_t) {}

}  // namespace

int main() {
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    std::printf("xgameruntime_offline_test: XGameEvent, XGameUi, XGameStreaming, XDisplay, XUserDevice\n");
    auto query = reinterpret_cast<Query>(rt::shims::find("xgameruntime.dll", "QueryApiImpl"));
    check(query != nullptr, "QueryApiImpl is registered");
    if (!query) return 1;

    // ---- XGameEvent ----
    void* events = nullptr;
    check(query(&kEventClass, kEventClass, &events) == S_OK && events, "XGameEventImpl for its class id");
    if (!events) return 1;
    check(at<EventWrite>(events, 3)(events, nullptr, "scid", "session", "GameStarted", "{}", "{}") == S_OK &&
              at<EventWrite>(events, 3)(events, nullptr, "scid", "session", "LevelLoaded", nullptr, nullptr) == S_OK,
          "XGameEventWrite succeeds offline");
    check(at<EventWrite>(events, 3)(events, nullptr, "scid", "session", nullptr, nullptr, nullptr) == E_INVALIDARG,
          "an event without a name is refused");
    check(rt::shims::xgameruntime_offline_game_events() == 2, "two events were counted");

    // ---- XGameUi ----
    void* ui = nullptr;
    void* ui_by_iface = nullptr;
    void* ui4 = nullptr;
    check(query(&kUiClass, kUiClass, &ui) == S_OK && ui && query(&kUiClass, kUiImpl, &ui_by_iface) == S_OK &&
              ui_by_iface == ui && query(&kUiClass, kUiImpl4, &ui4) == S_OK && ui4 == ui,
          "XGameUiImpl for its class id, IXGameUiImpl and IXGameUiImpl4");
    if (!ui) return 1;
    XAsyncBlock async{};
    uint32_t button = 99;
    check(at<Dialog>(ui, 3)(ui, &async, "Corrupt save", "The save could not be loaded.", "Retry", "Start new", nullptr,
                            1, 0) == S_OK &&
              finished(&async) && at<DialogResult>(ui, 4)(ui, &async, &button) == S_OK && button == 1,
          "a message dialog completes with its default button (Second)");
    async = XAsyncBlock{};
    check(at<Dialog>(ui, 3)(ui, &async, "t", "c", "OK", nullptr, nullptr, 3, 0) == E_INVALIDARG,
          "a default button past Third is refused");
    async = XAsyncBlock{};
    check(at<ErrorDialog>(ui, 14)(ui, &async, static_cast<HRESULT>(0x80830003), "save") == S_OK && finished(&async) &&
              at<AsyncOnly>(ui, 15)(ui, &async) == S_OK,
          "an error dialog is logged and dismissed");
    check(at<NoArgs>(ui, 7)(ui) == E_FAIL, "a profile card is not available offline");
    check(at<NoArgs>(ui, 20)(ui) == E_NOTIMPL, "an unknown slot answers E_NOTIMPL");

    // ---- XGameStreaming ----
    void* streaming = nullptr;
    void* streaming3 = nullptr;
    check(query(&kStreamingClass, kStreamingClass, &streaming) == S_OK && streaming &&
              query(&kStreamingClass, kStreamingImpl3, &streaming3) == S_OK && streaming3 == streaming,
          "XGameStreamingImpl for its class id and IXGameStreamingImpl3");
    if (!streaming) return 1;
    check(at<NoArgs>(streaming, 3)(streaming) == S_OK, "XGameStreamingInitialize succeeds");
    check(!at<Bool>(streaming, 5)(streaming), "XGameStreamingIsStreaming is false");
    check(at<Count>(streaming, 9)(streaming) == 0, "no streaming clients");
    check(at<Count>(streaming, 11)(streaming) == 0, "the connection state is the first enumerator");
    XTaskQueueRegistrationToken token{};
    check(at<Register>(streaming, 12)(streaming, nullptr, nullptr, reinterpret_cast<void*>(&on_state), &token) == S_OK &&
              token.token != 0 && at<Bool>(streaming, 13)(streaming),
          "connection-state registration hands out a token; unregistering is never pending");
    check(FAILED(at<NoArgs>(streaming, 10)(streaming)) && !at<Bool>(streaming, 21)(streaming),
          "a per-client call fails as an HRESULT and reads false as a bool");

    // ---- XDisplay ----
    void* display = nullptr;
    check(query(&kDisplayClass, kDisplayClass, &display) == S_OK && display, "XDisplayImpl for its class id");
    if (!display) return 1;
    float info[3] = {1, 2, 3};
    const uint32_t hdr = at<TryHdr>(display, 4)(display, 0, info);
    char hdr_env[4] = {};
    size_t hdr_len = 0;
    const bool want_hdr = getenv_s(&hdr_len, hdr_env, "LEONIDA_HDR") == 0 && std::strcmp(hdr_env, "1") == 0;
    check(want_hdr ? hdr == 1 && info[1] == 1000.0f : hdr == 2 && info[0] == 0 && info[1] == 0 && info[2] == 0,
          want_hdr ? "LEONIDA_HDR=1: HDR is enabled with the documented defaults"
                   : "XDisplayTryEnableHdrMode: Disabled (SDR output), the info cleared");
    check(at<TryHdr>(display, 4)(display, 1, nullptr) == hdr, "the info pointer is optional");

    // ---- XUserDevice ----
    void* device = nullptr;
    check(query(&kUserDeviceClass, kUserDeviceClass, &device) == S_OK && device, "XUserDeviceImpl for its class id");
    if (!device) return 1;
    uint8_t device_id[32] = {7};
    void* user = nullptr;
    check(at<FindUser>(device, 3)(device, device_id, &user) == S_OK && user, "XUserFindForDevice: the offline user");
    check(rt::shims::xgameruntime_user_open_handles() == 1, "the handle is an XUserHandle the title closes");
    at<CloseUser>(user_object(query), 4)(user_object(query), user);
    check(rt::shims::xgameruntime_user_open_handles() == 0, "XUserCloseHandle closes it");
    check(at<Register>(device, 4)(device, nullptr, nullptr, reinterpret_cast<void*>(&on_state), &token) == S_OK &&
              at<Unregister>(device, 5)(device, token, false),
          "device association registration and unregistration");
    wchar_t endpoint[8] = {L'x'};
    size_t used = 99;
    check(FAILED(at<Endpoint>(device, 6)(device, 1, 0, 8, endpoint, &used)) && endpoint[0] == 0 && used == 0,
          "no audio endpoint of the user's own");
    async = XAsyncBlock{};
    uint8_t found[32];
    std::memset(found, 0xAB, sizeof(found));
    bool zero = true;
    check(at<FindController>(device, 9)(device, reinterpret_cast<void*>(1), &async) == S_OK && finished(&async) &&
              at<FindControllerResult>(device, 10)(device, &async, found) == S_OK,
          "XUserFindControllerForUserWithUi completes without UI");
    for (uint8_t b : found) zero = zero && b == 0;
    check(zero, "with an all-zero device id, as on desktop");

    std::printf("xgameruntime_offline_test: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
