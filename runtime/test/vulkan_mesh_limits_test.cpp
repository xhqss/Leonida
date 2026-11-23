// leonida::mesh_limit_refusal with synthetic device limits: no GPU. A mesh PSO whose workgroup memory is over the
// device's mesh shared memory budget is refused before pipeline creation (a driver dies on that module), while one that
// fits, or a device whose limits were not read, is not. Mesh shared memory is a smaller budget than compute's.
#ifndef VK_NO_PROTOTYPES
#define VK_NO_PROTOTYPES
#endif
#include "../src/vulkan_mesh_limits.h"

#include <cstdio>

#define EXPECT(condition) do { \
    if (!(condition)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #condition); return 1; } \
} while (0)

using namespace leonida;

namespace {

// NVIDIA's mesh limits (maxMeshSharedMemorySize 28672; payload plus shared 28672).
VkPhysicalDeviceMeshShaderPropertiesEXT nvidia() {
    VkPhysicalDeviceMeshShaderPropertiesEXT l{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MESH_SHADER_PROPERTIES_EXT};
    l.maxMeshOutputVertices = 256;
    l.maxMeshOutputPrimitives = 256;
    l.maxMeshWorkGroupInvocations = 128;
    l.maxMeshWorkGroupSize[0] = 128;
    l.maxMeshWorkGroupSize[1] = l.maxMeshWorkGroupSize[2] = 128;
    l.maxMeshSharedMemorySize = 28672;
    l.maxMeshPayloadAndSharedMemorySize = 28672;
    return l;
}

ShaderEntry mesh(std::uint32_t lds_bytes) {
    ShaderEntry e;
    e.name = "ms_big";
    e.stage = "ms";
    e.local_size = {64, 1, 1};
    e.max_vertices = 64;
    e.max_primitives = 126;
    e.lds_bytes = lds_bytes;
    return e;
}

}  // namespace

int main() {
    const auto limits = nvidia();
    // At the budget: accepted. One word over: refused, naming the stage and both numbers.
    EXPECT(mesh_limit_refusal(mesh(28672), limits).empty());
    const std::string over = mesh_limit_refusal(mesh(28676), limits);
    EXPECT(over.find("ms_big") != std::string::npos && over.find("28676") != std::string::npos &&
           over.find("28672") != std::string::npos);
    // A 32 KiB groupshared mesh (fits compute on RADV/AMD Windows) is over NVIDIA's mesh budget but fits a device that
    // reports 32768 for both.
    EXPECT(!mesh_limit_refusal(mesh(32768), limits).empty());
    auto roomy = limits;
    roomy.maxMeshSharedMemorySize = roomy.maxMeshPayloadAndSharedMemorySize = 32768;
    EXPECT(mesh_limit_refusal(mesh(32768), roomy).empty());
    // The smaller of the two limits counts: shared memory plus payload is bounded by the second.
    auto tight = roomy;
    tight.maxMeshPayloadAndSharedMemorySize = 16384;
    EXPECT(!mesh_limit_refusal(mesh(20000), tight).empty());
    EXPECT(mesh_limit_refusal(mesh(16384), tight).empty());
    // No workgroup memory declared: never refused for it, whatever the limit.
    EXPECT(mesh_limit_refusal(mesh(0), limits).empty());
    // Limits that were not read (all zero) give no number to refuse on for memory, so a shader without outputs passes.
    VkPhysicalDeviceMeshShaderPropertiesEXT unread{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MESH_SHADER_PROPERTIES_EXT};
    ShaderEntry plain = mesh(4096);
    plain.max_vertices = plain.max_primitives = 0;
    plain.local_size = {0, 0, 0};
    EXPECT(mesh_limit_refusal(plain, unread).empty());
    // The checks that were already there still hold: too many vertices, a workgroup axis, total invocations.
    ShaderEntry many = mesh(0);
    many.max_vertices = 257;
    EXPECT(!mesh_limit_refusal(many, limits).empty());
    many = mesh(0);
    many.local_size = {129, 1, 1};
    EXPECT(!mesh_limit_refusal(many, limits).empty());
    many.local_size = {64, 4, 1};  // 256 invocations over the 128 allowed
    EXPECT(!mesh_limit_refusal(many, limits).empty());
    return 0;
}
