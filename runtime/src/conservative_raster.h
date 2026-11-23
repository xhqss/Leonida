#pragma once
#include <cmath>
#include <cstdint>

namespace leonida::conservative_raster {
// Tier 1 bounds the outer uncertainty to half a pixel and requires a 16.8 rasterization grid. Leave room for
// half a grid step at that minimum precision in addition to Vulkan's fixed primitive overestimation. Do not
// infer Tier 2/3 from degenerate-triangle support: their tighter bounds and InnerCoverage need separate work.
inline bool tier1(bool extension_enabled, std::uint32_t subpixel_bits, float overestimate) {
    constexpr float maximum = 0.5f - 1.0f / 512.0f;
    return extension_enabled && subpixel_bits >= 8 && std::isfinite(overestimate) &&
           overestimate >= 0.0f && overestimate <= maximum;
}
} // namespace leonida::conservative_raster
