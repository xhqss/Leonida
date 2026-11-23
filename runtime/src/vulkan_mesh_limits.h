// The device limits a mesh pipeline must fit, checked before vkCreate*Pipelines (docs/d3d12_vulkan.md, "Pipelines").
// Vulkan has no per-pipeline limit to set: a module whose output arrays, workgroup or shared memory are over the
// device's is invalid usage that spirv-val accepts and drivers die on (RADV, #1721, for compute), so the PSO is
// refused up front with the stage named.
#pragma once

#include "shader_library.h"

#include <vulkan/vulkan_core.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <string>

namespace leonida {

// Empty when the mesh entry fits the device's mesh-shader limits, else why not. `limits` is
// VkPhysicalDeviceMeshShaderPropertiesEXT as the device reports it, all zeros when it could not be asked.
//   - output vertices and primitives, workgroup size and invocations: as before.
//   - Workgroup memory (the entry's lds_bytes: the LDS array, %x_buf of --wave64-emulate and the mesh's own counter)
//     against maxMeshSharedMemorySize, and against maxMeshPayloadAndSharedMemorySize, which bounds shared memory
//     plus the task payload (the game has no amplification shaders, so the payload is 0). Mesh shared memory is a
//     separate, smaller budget than maxComputeSharedMemorySize: 28672 on NVIDIA, while a compute shader may have 48 KiB.
//     A limit of 0 means the properties were not read; the memory is not refused on a number nobody gave.
inline std::string mesh_limit_refusal(const ShaderEntry& entry, const VkPhysicalDeviceMeshShaderPropertiesEXT& limits) {
    const auto& n = entry.local_size;
    char text[384];
    if (entry.max_vertices > limits.maxMeshOutputVertices || entry.max_primitives > limits.maxMeshOutputPrimitives ||
        n[0] > limits.maxMeshWorkGroupSize[0] || n[1] > limits.maxMeshWorkGroupSize[1] ||
        n[2] > limits.maxMeshWorkGroupSize[2] ||
        std::uint64_t(n[0]) * n[1] * n[2] > limits.maxMeshWorkGroupInvocations) {
        std::snprintf(text, sizeof text,
                      "%s needs %u vertices, %u primitives, %ux%ux%u threads; the device allows %u, %u, %u invocations",
                      entry.name.c_str(), entry.max_vertices, entry.max_primitives, n[0], n[1], n[2],
                      limits.maxMeshOutputVertices, limits.maxMeshOutputPrimitives, limits.maxMeshWorkGroupInvocations);
        return text;
    }
    std::uint32_t shared_limit = limits.maxMeshSharedMemorySize;
    if (limits.maxMeshPayloadAndSharedMemorySize)
        shared_limit = shared_limit ? std::min(shared_limit, limits.maxMeshPayloadAndSharedMemorySize)
                                    : limits.maxMeshPayloadAndSharedMemorySize;
    if (entry.lds_bytes && shared_limit && entry.lds_bytes > shared_limit) {
        std::snprintf(text, sizeof text,
                      "%s declares %u bytes of workgroup memory, over the device's mesh shared memory limit %u "
                      "(maxMeshSharedMemorySize %u, maxMeshPayloadAndSharedMemorySize %u)",
                      entry.name.c_str(), entry.lds_bytes, shared_limit, limits.maxMeshSharedMemorySize,
                      limits.maxMeshPayloadAndSharedMemorySize);
        return text;
    }
    return {};
}

}  // namespace leonida
