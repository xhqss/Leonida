// D3D-27: D3D12 render passes through the layer. BeginRenderPass/EndRenderPass were the one M3 API the layer left
// unimplemented (docs/d3d12_vulkan.md section 6, M3). The layer renders through one dynamic-rendering scope per draw,
// so a pass applies what it asks for when it begins: BEGINNING_ACCESS CLEAR records the same clear the explicit call
// records, LOAD and PRESERVE keep the contents, and ENDING_ACCESS needs nothing on a desktop GPU. This test checks
// those three against pixels read back through CopyTextureRegion on the Vulkan device, and the error cases.
// No game data or shaders. Run explicitly (needs Vulkan hardware); LEONIDA_VK_VALIDATION=1 checks Vulkan usage.
#include "../shims/d3d12_com.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include <wrl/client.h>

#define CHECK(condition) do { \
    if (!(condition)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #condition); return false; } \
} while (0)

namespace {

using Microsoft::WRL::ComPtr;

constexpr UINT width = 4, height = 4;
constexpr UINT row_pitch = 256;  // D3D12_TEXTURE_DATA_PITCH_ALIGNMENT
constexpr UINT image_bytes = row_pitch * height;

struct Event {
    HANDLE handle = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    ~Event() { if (handle) CloseHandle(handle); }
};

struct Resources {
    ComPtr<ID3D12Device> device;
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList4> list;
    ComPtr<ID3D12Fence> fence;
    ComPtr<ID3D12Resource> color[2], depth, readback;
    ComPtr<ID3D12DescriptorHeap> rtv_heap, dsv_heap;
    D3D12_CPU_DESCRIPTOR_HANDLE rtv[2]{}, dsv{};
};

D3D12_RESOURCE_DESC texture_desc(DXGI_FORMAT format, D3D12_RESOURCE_FLAGS flags) {
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.Format = format;
    desc.SampleDesc.Count = 1;
    desc.Flags = flags;
    return desc;
}

bool setup(Resources& r) {
    if (FAILED(rt::d3d12::create_vulkan_device_standalone(IID_PPV_ARGS(&r.device)))) return false;
    D3D12_COMMAND_QUEUE_DESC queue{};
    CHECK(SUCCEEDED(r.device->CreateCommandQueue(&queue, IID_PPV_ARGS(&r.queue))));
    CHECK(SUCCEEDED(r.device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&r.allocator))));
    CHECK(SUCCEEDED(r.device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, r.allocator.Get(), nullptr,
                                                IID_PPV_ARGS(&r.list))));
    CHECK(SUCCEEDED(r.device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&r.fence))));

    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    for (auto& color : r.color) {
        const auto desc = texture_desc(DXGI_FORMAT_R8G8B8A8_UNORM, D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET);
        CHECK(SUCCEEDED(r.device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
                                                          D3D12_RESOURCE_STATE_RENDER_TARGET, nullptr,
                                                          IID_PPV_ARGS(&color))));
    }
    const auto depth_desc = texture_desc(DXGI_FORMAT_D32_FLOAT, D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL);
    D3D12_CLEAR_VALUE depth_clear{};
    depth_clear.Format = DXGI_FORMAT_D32_FLOAT;
    depth_clear.DepthStencil.Depth = 1.0f;
    CHECK(SUCCEEDED(r.device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &depth_desc,
                                                      D3D12_RESOURCE_STATE_DEPTH_WRITE, &depth_clear,
                                                      IID_PPV_ARGS(&r.depth))));

    D3D12_RESOURCE_DESC buffer{};
    buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    buffer.Width = 3ull * image_bytes;
    buffer.Height = buffer.DepthOrArraySize = buffer.MipLevels = 1;
    buffer.SampleDesc.Count = 1;
    buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    heap.Type = D3D12_HEAP_TYPE_READBACK;
    CHECK(SUCCEEDED(r.device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
                                                      D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                                      IID_PPV_ARGS(&r.readback))));

    const D3D12_DESCRIPTOR_HEAP_DESC rtv_desc{D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 2, D3D12_DESCRIPTOR_HEAP_FLAG_NONE, 0};
    CHECK(SUCCEEDED(r.device->CreateDescriptorHeap(&rtv_desc, IID_PPV_ARGS(&r.rtv_heap))));
    const D3D12_DESCRIPTOR_HEAP_DESC dsv_desc{D3D12_DESCRIPTOR_HEAP_TYPE_DSV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_NONE, 0};
    CHECK(SUCCEEDED(r.device->CreateDescriptorHeap(&dsv_desc, IID_PPV_ARGS(&r.dsv_heap))));
    const auto increment = r.device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    r.rtv[0] = r.rtv_heap->GetCPUDescriptorHandleForHeapStart();
    r.rtv[1] = r.rtv[0];
    r.rtv[1].ptr += increment;
    for (UINT i = 0; i < 2; ++i) r.device->CreateRenderTargetView(r.color[i].Get(), nullptr, r.rtv[i]);
    r.dsv = r.dsv_heap->GetCPUDescriptorHandleForHeapStart();
    r.device->CreateDepthStencilView(r.depth.Get(), nullptr, r.dsv);
    return true;
}

// Copies the three targets into the readback buffer at one image each and reads them back as RGBA8 and float dwords.
bool read_back(Resources& r, std::vector<std::uint32_t>& color0, std::vector<std::uint32_t>& color1,
               std::vector<float>& depth) {
    D3D12_RESOURCE_BARRIER barriers[3]{};
    for (UINT i = 0; i < 3; ++i) {
        barriers[i].Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barriers[i].Transition = {i < 2 ? r.color[i].Get() : r.depth.Get(),
                                  D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES,
                                  i < 2 ? D3D12_RESOURCE_STATE_RENDER_TARGET : D3D12_RESOURCE_STATE_DEPTH_WRITE,
                                  D3D12_RESOURCE_STATE_COPY_SOURCE};
    }
    r.list->ResourceBarrier(3, barriers);
    D3D12_TEXTURE_COPY_LOCATION dst{};
    dst.pResource = r.readback.Get();
    dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint.Footprint = {DXGI_FORMAT_R8G8B8A8_UNORM, width, height, 1, row_pitch};
    for (UINT i = 0; i < 3; ++i) {
        D3D12_TEXTURE_COPY_LOCATION src{};
        src.pResource = i < 2 ? r.color[i].Get() : r.depth.Get();
        src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        dst.PlacedFootprint.Offset = std::uint64_t(i) * image_bytes;
        r.list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    }
    CHECK(SUCCEEDED(r.list->Close()));
    ID3D12CommandList* lists[]{r.list.Get()};
    r.queue->ExecuteCommandLists(1, lists);
    CHECK(SUCCEEDED(r.queue->Signal(r.fence.Get(), 1)));
    Event event;
    CHECK(SUCCEEDED(r.fence->SetEventOnCompletion(1, event.handle)));
    CHECK(WaitForSingleObject(event.handle, 30000) == WAIT_OBJECT_0);

    void* mapped = nullptr;
    const D3D12_RANGE range{0, 3ull * image_bytes};
    CHECK(SUCCEEDED(r.readback->Map(0, &range, &mapped)));
    const auto* bytes = static_cast<const std::uint8_t*>(mapped);
    const auto words = [&](std::size_t base, auto& out, auto convert) {
        out.reserve(width * height);
        for (UINT y = 0; y < height; ++y)
            for (UINT x = 0; x < width; ++x) {
                std::uint32_t value = 0;
                std::memcpy(&value, bytes + base + y * row_pitch + x * 4, 4);
                out.push_back(convert(value));
            }
    };
    words(0, color0, [](std::uint32_t v) { return v; });
    words(image_bytes, color1, [](std::uint32_t v) { return v; });
    words(2ull * image_bytes, depth, [](std::uint32_t v) {
        float f = 0;
        std::memcpy(&f, &v, 4);
        return f;
    });
    const D3D12_RANGE empty{0, 0};
    r.readback->Unmap(0, &empty);
    return true;
}

bool all_equal(const std::vector<std::uint32_t>& pixels, std::uint32_t want) {
    for (auto p : pixels) if (p != want) return false;
    return true;
}

// 0xRRGGBBAA as a little-endian dword in an R8G8B8A8_UNORM texel: bytes R, G, B, A.
constexpr std::uint32_t rgba8(std::uint8_t r, std::uint8_t g, std::uint8_t b, std::uint8_t a) {
    return std::uint32_t(r) | (std::uint32_t(g) << 8) | (std::uint32_t(b) << 16) | (std::uint32_t(a) << 24);
}

bool begin_pass(Resources& r, D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE type, const FLOAT* color0,
                const FLOAT* color1, const FLOAT* depth, bool with_depth = true) {
    D3D12_RENDER_PASS_RENDER_TARGET_DESC targets[2]{};
    targets[0].cpuDescriptor = r.rtv[0];
    targets[1].cpuDescriptor = r.rtv[1];
    const FLOAT* colors[2]{color0, color1};
    for (UINT i = 0; i < 2; ++i) {
        targets[i].BeginningAccess.Type = type;
        if (type == D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_CLEAR) {
            targets[i].BeginningAccess.Clear.ClearValue.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
            for (UINT c = 0; c < 4; ++c) targets[i].BeginningAccess.Clear.ClearValue.Color[c] = colors[i][c];
        }
        targets[i].EndingAccess.Type = D3D12_RENDER_PASS_ENDING_ACCESS_TYPE_PRESERVE;
    }
    D3D12_RENDER_PASS_DEPTH_STENCIL_DESC ds{};
    ds.cpuDescriptor = r.dsv;
    ds.DepthBeginningAccess.Type = type;
    ds.DepthEndingAccess.Type = D3D12_RENDER_PASS_ENDING_ACCESS_TYPE_PRESERVE;
    ds.StencilBeginningAccess.Type = D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_NO_ACCESS;
    ds.StencilEndingAccess.Type = D3D12_RENDER_PASS_ENDING_ACCESS_TYPE_PRESERVE;
    if (type == D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_CLEAR && depth) {
        ds.DepthBeginningAccess.Clear.ClearValue.Format = DXGI_FORMAT_D32_FLOAT;
        ds.DepthBeginningAccess.Clear.ClearValue.DepthStencil.Depth = *depth;
    }
    r.list->BeginRenderPass(2, targets, with_depth ? &ds : nullptr, D3D12_RENDER_PASS_FLAG_NONE);
    r.list->EndRenderPass();
    return true;
}

// CLEAR at the beginning clears both targets and the depth, LOAD and PRESERVE keep what was there (a pre-pass clear),
// and NO_ACCESS leaves the contents undefined: on a desktop GPU they are still there, which the layer documents.
bool case_contents(bool load) {
    Resources r;
    CHECK(setup(r));
    const FLOAT red[4]{1, 0, 0, 1}, blue[4]{0, 0, 1, 1}, green[4]{0, 1, 0, 1}, depth{0.25f};
    if (load) {
        r.list->ClearRenderTargetView(r.rtv[0], green, 0, nullptr);
        r.list->ClearRenderTargetView(r.rtv[1], green, 0, nullptr);
    }
    begin_pass(r, load ? D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_PRESERVE
                       : D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_CLEAR,
               red, blue, &depth, false);
    std::vector<std::uint32_t> color0, color1;
    std::vector<float> depth_values;
    CHECK(read_back(r, color0, color1, depth_values));
    CHECK(all_equal(color0, load ? rgba8(0, 255, 0, 255) : rgba8(255, 0, 0, 255)));
    CHECK(all_equal(color1, load ? rgba8(0, 255, 0, 255) : rgba8(0, 0, 255, 255)));
    return true;
}

bool case_depth() {
    Resources r;
    CHECK(setup(r));
    const FLOAT red[4]{1, 0, 0, 1}, blue[4]{0, 0, 1, 1}, depth{0.25f};
    begin_pass(r, D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_CLEAR, red, blue, &depth);
    std::vector<std::uint32_t> color0, color1;
    std::vector<float> depth_values;
    CHECK(read_back(r, color0, color1, depth_values));
    CHECK(all_equal(color0, rgba8(255, 0, 0, 255)));
    CHECK(all_equal(color1, rgba8(0, 0, 255, 255)));
    for (auto value : depth_values) CHECK(value == 0.25f);
    return true;
}

// A pass does not nest, EndRenderPass needs an open pass, a pass left open invalidates Close, and suspend/resume is
// refused instead of half-implemented. Each failure is D3D12's E_INVALIDARG at Close, as the layer reports others.
bool case_errors() {
    Resources r;
    CHECK(setup(r));
    const FLOAT red[4]{1, 0, 0, 1};
    D3D12_RENDER_PASS_RENDER_TARGET_DESC target{};
    target.cpuDescriptor = r.rtv[0];
    target.BeginningAccess.Type = D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_NO_ACCESS;
    target.EndingAccess.Type = D3D12_RENDER_PASS_ENDING_ACCESS_TYPE_PRESERVE;
    const auto reset = [&] { CHECK(SUCCEEDED(r.list->Reset(r.allocator.Get(), nullptr))); };

    r.list->EndRenderPass();  // no pass open
    CHECK(FAILED(r.list->Close()));

    reset();
    r.list->BeginRenderPass(1, &target, nullptr, D3D12_RENDER_PASS_FLAG_NONE);
    r.list->BeginRenderPass(1, &target, nullptr, D3D12_RENDER_PASS_FLAG_NONE);  // nested
    r.list->EndRenderPass();
    CHECK(FAILED(r.list->Close()));

    reset();
    target.BeginningAccess.Type = D3D12_RENDER_PASS_BEGINNING_ACCESS_TYPE_CLEAR;
    target.BeginningAccess.Clear.ClearValue.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    for (UINT c = 0; c < 4; ++c) target.BeginningAccess.Clear.ClearValue.Color[c] = red[c];
    r.list->BeginRenderPass(1, &target, nullptr, D3D12_RENDER_PASS_FLAG_RESUMING_PASS);
    r.list->EndRenderPass();
    CHECK(FAILED(r.list->Close()));

    reset();
    r.list->BeginRenderPass(1, &target, nullptr, D3D12_RENDER_PASS_FLAG_NONE);
    CHECK(FAILED(r.list->Close()));  // the pass was never ended

    reset();  // the control: a matched pair closes cleanly
    r.list->BeginRenderPass(1, &target, nullptr, D3D12_RENDER_PASS_FLAG_NONE);
    r.list->EndRenderPass();
    CHECK(SUCCEEDED(r.list->Close()));
    return true;
}

}  // namespace

int main() {
    if (!case_contents(false)) return 1;
    if (!case_contents(true)) return 1;
    if (!case_depth()) return 1;
    if (!case_errors()) return 1;
    std::puts("render pass: CLEAR and PRESERVE behave, the depth clears, and the error cases are refused");
    return 0;
}
