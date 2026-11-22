// Joins pipeline caches written by separate vk_pipeline_check processes: vk_cache_merge <destination.bin> <source.bin>...
// The AMD Windows driver (32.0.32029.2010) serializes pipeline compilation inside one process, so the corpus is
// compiled faster by N processes that each own a cache file (vk_pipeline_check --shard I/N); vkMergePipelineCaches
// then folds those files into the one cache the runtime ships.
//   - the destination is loaded when it exists and rewritten with the merged data through pipeline_cache_file.h
//   - a source whose header names another vendor, device or driver is refused rather than merged (that merge is
//     invalid usage); an empty source is skipped
#include "vulkan_loader.h"
#include "vk_device_pick.h"
#include "pipeline_cache_file.h"
#include "pipeline_cache_snapshot.h"

#include <cstddef>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

namespace {

std::vector<char> read_file(const std::string& path) {
    std::ifstream file(path, std::ios::binary);
    return std::vector<char>(std::istreambuf_iterator<char>(file), {});
}

// VkPipelineCacheHeaderVersionOne is the only header a portable cache may carry. Data without one cannot be
// checked; data too short to hold a header is treated as empty.
bool same_driver_cache(const std::vector<char>& data, const VkPhysicalDeviceProperties& props) {
    if (data.size() < sizeof(VkPipelineCacheHeaderVersionOne)) return data.empty();
    VkPipelineCacheHeaderVersionOne header{};
    std::memcpy(&header, data.data(), sizeof(header));
    return header.headerVersion == VK_PIPELINE_CACHE_HEADER_VERSION_ONE && header.vendorID == props.vendorID &&
           header.deviceID == props.deviceID &&
           std::memcmp(header.pipelineCacheUUID, props.pipelineCacheUUID, VK_UUID_SIZE) == 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: vk_cache_merge <destination.bin> <source.bin> [source.bin ...]\n");
        return 2;
    }
    const std::string destination = argv[1];
    std::vector<std::string> paths(argv + 1, argv + argc);
    std::vector<std::vector<char>> data;
    data.reserve(paths.size());
    for (const auto& path : paths) data.push_back(read_file(path));
    std::fprintf(stderr, "destination %s: %zu bytes\n", destination.c_str(), data[0].size());

    auto gipa = load_vulkan();
    if (!gipa) return 1;
    VkInstance inst = VK_NULL_HANDLE;
#define LOADI(name) auto name = reinterpret_cast<PFN_##name>(gipa(inst, #name))
    LOADI(vkCreateInstance);
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.apiVersion = VK_API_VERSION_1_3;
    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ici.pApplicationInfo = &app;
    if (vkCreateInstance(&ici, nullptr, &inst)) return 1;
    LOADI(vkEnumeratePhysicalDevices); LOADI(vkGetPhysicalDeviceProperties); LOADI(vkCreateDevice);
    LOADI(vkGetDeviceProcAddr);
    uint32_t n = 0;
    vkEnumeratePhysicalDevices(inst, &n, nullptr);
    if (n == 0) { std::fprintf(stderr, "no Vulkan physical devices found\n"); return 1; }
    std::vector<VkPhysicalDevice> pds(n);
    vkEnumeratePhysicalDevices(inst, &n, pds.data());
    std::vector<VkPhysicalDeviceProperties> candidates(pds.size());
    for (std::size_t i = 0; i < pds.size(); ++i) vkGetPhysicalDeviceProperties(pds[i], &candidates[i]);
    const std::size_t chosen = leonida::vk_test::pick_device(candidates);
    if (chosen >= pds.size()) { leonida::vk_test::report_no_device(); return 1; }
    VkPhysicalDevice pd = pds[chosen];
    const VkPhysicalDeviceProperties& props = candidates[chosen];
    std::fprintf(stderr, "device: %s\n", props.deviceName);

    float prio = 1;
    VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qci.queueFamilyIndex = 0; qci.queueCount = 1; qci.pQueuePriorities = &prio;
    VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    dci.queueCreateInfoCount = 1; dci.pQueueCreateInfos = &qci;
    VkDevice dev = VK_NULL_HANDLE;
    if (VkResult r = vkCreateDevice(pd, &dci, nullptr, &dev)) {
        std::fprintf(stderr, "vkCreateDevice %d\n", r);
        return 1;
    }
#define LOADD(name) auto name = reinterpret_cast<PFN_##name>(vkGetDeviceProcAddr(dev, #name))
    LOADD(vkCreatePipelineCache); LOADD(vkMergePipelineCaches); LOADD(vkGetPipelineCacheData);
    LOADD(vkDestroyPipelineCache);

    VkPipelineCacheCreateInfo pci{VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO};
    pci.initialDataSize = data[0].size();
    pci.pInitialData = data[0].empty() ? nullptr : data[0].data();
    VkPipelineCache dst = VK_NULL_HANDLE;
    if (VkResult r = vkCreatePipelineCache(dev, &pci, nullptr, &dst)) {
        std::fprintf(stderr, "vkCreatePipelineCache(%s) failed: %d\n", destination.c_str(), r);
        return 1;
    }

    std::vector<VkPipelineCache> sources;
    for (std::size_t i = 1; i < paths.size(); i++) {
        if (data[i].empty()) {
            std::fprintf(stderr, "skip %s: empty\n", paths[i].c_str());
            continue;
        }
        if (!same_driver_cache(data[i], props)) {
            std::fprintf(stderr, "skip %s: header names another vendor, device or driver\n", paths[i].c_str());
            continue;
        }
        pci.initialDataSize = data[i].size();
        pci.pInitialData = data[i].data();
        VkPipelineCache src = VK_NULL_HANDLE;
        if (VkResult r = vkCreatePipelineCache(dev, &pci, nullptr, &src)) {
            std::fprintf(stderr, "skip %s: vkCreatePipelineCache %d\n", paths[i].c_str(), r);
            continue;
        }
        std::fprintf(stderr, "source %s: %zu bytes\n", paths[i].c_str(), data[i].size());
        sources.push_back(src);
    }
    if (sources.empty()) {
        std::fprintf(stderr, "nothing to merge into %s\n", destination.c_str());
        return 1;
    }
    const VkResult merged = vkMergePipelineCaches(dev, dst, uint32_t(sources.size()), sources.data());
    if (merged) {
        std::fprintf(stderr, "vkMergePipelineCaches failed: %d\n", merged);
        return 1;
    }
    for (VkPipelineCache src : sources) vkDestroyPipelineCache(dev, src, nullptr);

    const std::vector<char> out = [&] {
        try {
            return pipeline_cache_snapshot([&](size_t* size, void* dest) {
                return vkGetPipelineCacheData(dev, dst, size, dest);
            });
        } catch (const std::exception& e) {
            std::fprintf(stderr, "reading the merged cache failed: %s\n", e.what());
            return std::vector<char>{};
        }
    }();
    if (out.empty()) return 1;
    if (auto error = write_pipeline_cache_file(destination, out.data(), out.size())) {
        std::fprintf(stderr, "pipeline cache save to %s failed: %s\n", destination.c_str(), error.message().c_str());
        return 1;
    }
    std::printf("merged %zu caches: %zu -> %zu bytes -> %s\n", sources.size(), data[0].size(), out.size(),
                destination.c_str());
    vkDestroyPipelineCache(dev, dst, nullptr);
    return 0;
}
