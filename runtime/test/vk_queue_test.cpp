// D3D12 copy and compute queues on their own Vulkan queues, with cross-queue fences. No shaders or game data.
//
// The device gets a queue per D3D12 type (dedicated families when the GPU has them). A copy queue uploads a buffer
// and a texture, the way asset streaming does, signals a fence, and the direct queue waits on that fence on the GPU
// before reading both back. The compute queue does the same handoff the other way. A copy queue must refuse a
// render-target clear. Set LEONIDA_VK_VALIDATION=1 to run under VK_LAYER_KHRONOS_validation; any validation error
// fails the test.
#include "vk_test_device.h"
#include "../src/vulkan_backend.h"

#include <array>
#include <cstdint>
#include <cstring>
#include <vector>

#define EXPECT(condition) do { \
    if (!(condition)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #condition); return 1; } \
} while (0)

namespace {

using namespace leonida;
using Kind = VulkanQueue::QueueKind;

const char* flags_text(VkQueueFlags f) {
    static char buf[32];
    std::snprintf(buf, sizeof buf, "%s%s%s", f & VK_QUEUE_GRAPHICS_BIT ? "G" : "", f & VK_QUEUE_COMPUTE_BIT ? "C" : "",
                  f & VK_QUEUE_TRANSFER_BIT ? "T" : "");
    return buf;
}

int check_picker() {
    // Synthetic family layouts: the picker's choices don't depend on the GPU running the test.
    auto fam = [](VkQueueFlags flags, std::uint32_t granularity = 1) {
        VkQueueFamilyProperties p{};
        p.queueFlags = flags;
        p.queueCount = 1;
        p.minImageTransferGranularity = {granularity, granularity, granularity};
        return p;
    };
    constexpr VkQueueFlags G = VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT | VK_QUEUE_TRANSFER_BIT;
    constexpr VkQueueFlags C = VK_QUEUE_COMPUTE_BIT | VK_QUEUE_TRANSFER_BIT;
    constexpr VkQueueFlags T = VK_QUEUE_TRANSFER_BIT;
    {
        const std::array f{fam(G), fam(T), fam(C)};  // the common NVIDIA layout
        const auto q = pick_queue_families(f, 0);
        EXPECT(q.direct == 0 && q.compute == 2 && q.copy == 1);
    }
    {
        const std::array f{fam(G)};  // one family for everything (integrated GPUs, llvmpipe)
        const auto q = pick_queue_families(f, 0);
        EXPECT(q.direct == 0 && q.compute == 0 && q.copy == 0);
    }
    {
        const std::array f{fam(G), fam(C)};  // no DMA family: copies go to async compute
        const auto q = pick_queue_families(f, 0);
        EXPECT(q.compute == 1 && q.copy == 1);
    }
    {
        const std::array f{fam(G), fam(T, 8), fam(C)};  // a coarse DMA family is skipped
        const auto q = pick_queue_families(f, 0);
        EXPECT(q.copy == 2);
    }
    // D3D12 tile mapping is legal on all queue kinds. Dedicated async queues that cannot
    // bind sparse pages must use the graphics family when sparse buffers are offered.
    {
        constexpr VkQueueFlags S = VK_QUEUE_SPARSE_BINDING_BIT;
        const std::array f{fam(G | S), fam(T), fam(C)};
        const auto preferred = pick_queue_families(f, 0);
        EXPECT(preferred.compute == 2 && preferred.copy == 1);
        const auto corrected = sparse_capable_queue_families(f, preferred, true);
        EXPECT(corrected.direct == 0 && corrected.compute == 0 && corrected.copy == 0);
        const auto no_sparse = sparse_capable_queue_families(f, preferred, false);
        EXPECT(no_sparse.compute == 2 && no_sparse.copy == 1);
    }
    {
        constexpr VkQueueFlags S = VK_QUEUE_SPARSE_BINDING_BIT;
        const std::array f{fam(G | S), fam(T | S), fam(C | S)};
        const auto preferred = pick_queue_families(f, 0);
        const auto corrected = sparse_capable_queue_families(f, preferred, true);
        EXPECT(corrected.compute == 2 && corrected.copy == 1);  // preserve dedicated queues
    }
    {
        constexpr VkQueueFlags S = VK_QUEUE_SPARSE_BINDING_BIT;
        const std::array f{fam(G | S), fam(T), fam(C | S)};
        const auto preferred = pick_queue_families(f, 0);
        const auto corrected = sparse_capable_queue_families(f, preferred, true);
        EXPECT(corrected.compute == 2 && corrected.copy == 0);  // mixed capabilities
    }
    std::printf("queue family picker: 7 layouts ok\n");
    return 0;
}

constexpr std::uint32_t kSize = 64 * 1024;
constexpr std::uint32_t kTex = 64;  // 64x64 RGBA8: row pitch 256, already aligned

std::uint8_t pattern(std::uint32_t i, std::uint8_t seed) { return std::uint8_t(i * 31 + seed); }

}  // namespace

int main() {
    if (check_picker()) return 1;

    vktest::Device d;
    d.all_queue_types = true;
    if (!d.create()) {
        std::printf("SKIP: no usable Vulkan device\n");
        return 77;
    }
    std::printf("families: direct %u (%s)", d.family, flags_text(d.families[d.family].queueFlags));
    std::printf(", compute %u (%s)", d.compute_family, flags_text(d.families[d.compute_family].queueFlags));
    std::printf(", copy %u (%s)\n", d.copy_family, flags_text(d.families[d.copy_family].queueFlags));

    VulkanContext context(d.device_proc, d.device, true);
    EXPECT(context.loaded());
    VulkanQueue direct(context, d.queue, d.family, Kind::direct);
    VulkanQueue compute(context, d.compute_queue, d.compute_family, Kind::compute);
    VulkanQueue copy(context, d.copy_queue, d.copy_family, Kind::copy);
    EXPECT(direct.ok() && compute.ok() && copy.ok());

    // Buffers: an upload source, a GPU-side destination and a readback, all host-visible for the test.
    void *upload_map = nullptr, *mid_map = nullptr, *readback_map = nullptr, *tex_readback_map = nullptr;
    const VkBufferUsageFlags usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    VkBuffer upload = d.buffer(kSize, usage, &upload_map);
    VkBuffer mid = d.buffer(kSize, usage, &mid_map);
    VkBuffer readback = d.buffer(kSize, usage, &readback_map);
    VkBuffer tex_readback = d.buffer(kTex * kTex * 4, usage, &tex_readback_map);
    VkImage texture = d.image(VK_FORMAT_R8G8B8A8_UNORM, kTex, kTex, 1, VK_IMAGE_USAGE_SAMPLED_BIT);
    EXPECT(upload && mid && readback && tex_readback && texture);
    auto* up = static_cast<std::uint8_t*>(upload_map);
    for (std::uint32_t i = 0; i < kSize; ++i) up[i] = pattern(i, 7);

    const ResourceId upload_id = 1, mid_id = 2, readback_id = 3, tex_id = 4, tex_readback_id = 5;
    auto add_buffer = [&](ResourceId id, VkBuffer b, VkDeviceSize size) {
        ResourceBinding r;
        r.buffer = b;
        r.size = size;
        context.add_resource(id, r);
    };
    add_buffer(upload_id, upload, kSize);
    add_buffer(mid_id, mid, kSize);
    add_buffer(readback_id, readback, kSize);
    add_buffer(tex_readback_id, tex_readback, kTex * kTex * 4);
    ResourceBinding tex;
    tex.image = texture;
    tex.format = VK_FORMAT_R8G8B8A8_UNORM;
    tex.width = kTex;
    tex.height = kTex;
    // As the device registers a texture: tracked, still undefined, in D3D12's common state. Its first use is the
    // copy-queue upload, which can't zero-fill it first.
    context.add_resource(tex_id, tex, VK_IMAGE_LAYOUT_UNDEFINED);

    VulkanFence fence(context, 0);
    EXPECT(fence.semaphore());

    // 1. Copy queue: buffer upload and a texture upload from the same staging buffer (footprint copy), then signal.
    CommandList on_copy;
    EXPECT(on_copy.copy_buffer({mid_id, 0, upload_id, 0, kSize}));
    CopyTexture upload_tex{};
    upload_tex.dst = {tex_id, false, 0, 0, 0, 0, 0, 0};
    upload_tex.src = {upload_id, true, 0, 0, kTex, kTex, 1, kTex * 4};
    EXPECT(on_copy.copy_texture(upload_tex));
    EXPECT(on_copy.close());
    const CommandList* copy_lists[] = {&on_copy};
    EXPECT(copy.execute(copy_lists) == ReplayResult::success);
    EXPECT(copy.signal(fence.semaphore(), 1));

    // 2. Direct queue: wait for the copy queue on the GPU, then read both back.
    direct.wait(fence.semaphore(), 1);
    CommandList on_direct;
    EXPECT(on_direct.copy_buffer({readback_id, 0, mid_id, 0, kSize}));
    CopyTexture read_tex{};
    read_tex.dst = {tex_readback_id, true, 0, 0, kTex, kTex, 1, kTex * 4};
    read_tex.src = {tex_id, false, 0, 0, 0, 0, 0, 0};
    EXPECT(on_direct.copy_texture(read_tex));
    EXPECT(on_direct.close());
    const CommandList* direct_lists[] = {&on_direct};
    EXPECT(direct.execute(direct_lists) == ReplayResult::success);
    EXPECT(direct.signal(fence.semaphore(), 2));
    EXPECT(fence.wait(2, 5'000'000'000ull));

    std::uint32_t bad = 0;
    auto* rb = static_cast<std::uint8_t*>(readback_map);
    for (std::uint32_t i = 0; i < kSize; ++i) bad += rb[i] != pattern(i, 7);
    auto* tr = static_cast<std::uint8_t*>(tex_readback_map);
    for (std::uint32_t i = 0; i < kTex * kTex * 4; ++i) bad += tr[i] != pattern(i, 7);
    std::printf("copy -> direct: buffer and texture upload read back, %u bad bytes\n", bad);
    EXPECT(bad == 0);

    // 3. Compute queue the other way: wait for the direct queue, copy, signal; the host waits on the fence.
    for (std::uint32_t i = 0; i < kSize; ++i) up[i] = pattern(i, 99);
    CommandList on_direct2;
    EXPECT(on_direct2.copy_buffer({mid_id, 0, upload_id, 0, kSize}));
    EXPECT(on_direct2.close());
    const CommandList* direct_lists2[] = {&on_direct2};
    EXPECT(direct.execute(direct_lists2) == ReplayResult::success);
    EXPECT(direct.signal(fence.semaphore(), 3));
    compute.wait(fence.semaphore(), 3);
    CommandList on_compute;
    EXPECT(on_compute.copy_buffer({readback_id, 0, mid_id, 0, kSize}));
    EXPECT(on_compute.close());
    const CommandList* compute_lists[] = {&on_compute};
    EXPECT(compute.execute(compute_lists) == ReplayResult::success);
    EXPECT(compute.signal(fence.semaphore(), 4));
    EXPECT(fence.wait(4, 5'000'000'000ull));
    bad = 0;
    for (std::uint32_t i = 0; i < kSize; ++i) bad += rb[i] != pattern(i, 99);
    std::printf("direct -> compute: %u bad bytes\n", bad);
    EXPECT(bad == 0);

    // 4. A copy queue refuses a render-target clear and submits nothing.
    VkImage rt_image = d.image(VK_FORMAT_R8G8B8A8_UNORM, 8, 8, 1, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT);
    ResourceBinding rt;
    rt.image = rt_image;
    rt.format = VK_FORMAT_R8G8B8A8_UNORM;
    rt.width = rt.height = 8;
    context.add_resource(6, rt, VK_IMAGE_LAYOUT_UNDEFINED);
    CommandList clear;
    TargetView view{};
    view.resource = 6;
    view.layer_count = 1;
    EXPECT(clear.clear_render_target(view, ClearColor{}));
    EXPECT(clear.close());
    const CommandList* clear_lists[] = {&clear};
    EXPECT(copy.execute(clear_lists) == ReplayResult::unsupported);
    EXPECT(compute.execute(clear_lists) == ReplayResult::unsupported);
    std::printf("copy and compute queues refuse a render-target clear\n");

    EXPECT(direct.idle() && compute.idle() && copy.idle());
    if (d.validation) {
        std::printf("validation errors: %u\n", d.validation_errors());
        EXPECT(d.validation_errors() == 0);
    }
    std::printf("vk_queue_test: OK\n");
    return 0;
}
