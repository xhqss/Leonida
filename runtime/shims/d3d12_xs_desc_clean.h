// The PC form of a resource description the retail title builds for the console, for the Xbox-ABI device proxy.
//
// The title's D3D12_RESOURCE_DESC carries console values the PC device cannot take: the layout word is 0x101 (tiled)
// or 0x1 (row-major buffer), and the flags have bits above the PC ones set (docs/retail_boot_chain.md, #6982). Slot 76
// replaces the layout word the same way before it forwards to CreateCommittedResource; slot 25 and the slot 127
// stand-in need the same cleaning. The title's own description is never written: a caller gets a copy.
#pragma once

#include <windows.h>
#include <d3d12.h>

#include <cstdint>

namespace rt::d3d12_xs {

// The D3D12_RESOURCE_FLAGS bits the PC runtime defines; every other bit is a console one.
constexpr std::uint32_t kPcResourceFlagsMask =
    D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET | D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL |
    D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS | D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE |
    D3D12_RESOURCE_FLAG_ALLOW_CROSS_ADAPTER | D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS |
    D3D12_RESOURCE_FLAG_VIDEO_DECODE_REFERENCE_ONLY | D3D12_RESOURCE_FLAG_VIDEO_ENCODE_REFERENCE_ONLY |
    D3D12_RESOURCE_FLAG_RAYTRACING_ACCELERATION_STRUCTURE;

constexpr auto kXboxTiledResourceLayout = static_cast<D3D12_TEXTURE_LAYOUT>(0x101);

// Formats the PC runtime stores as depth/stencil. It gives none of them a UAV, so ALLOW_UNORDERED_ACCESS on such a
// description is invalid there: measured on 1.0.2.1, a 2560x1440 R32G8X24_TYPELESS description with
// ALLOW_DEPTH_STENCIL | ALLOW_UNORDERED_ACCESS makes GetResourceAllocationInfo answer UINT64_MAX, and the same
// description without the UAV bit is accepted (0x1330000 bytes). The title then asserts on the UINT64_MAX right after
// the call (its check at game_scarlett_master.exe+0x344196), so the bit has to go.
constexpr bool is_depth_format(DXGI_FORMAT format) noexcept {
    switch (format) {
        case DXGI_FORMAT_R32G8X24_TYPELESS:
        case DXGI_FORMAT_D32_FLOAT_S8X24_UINT:
        case DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS:
        case DXGI_FORMAT_R24G8_TYPELESS:
        case DXGI_FORMAT_D24_UNORM_S8_UINT:
        case DXGI_FORMAT_R24_UNORM_X8_TYPELESS:
        case DXGI_FORMAT_R16_TYPELESS:
        case DXGI_FORMAT_D16_UNORM:
        case DXGI_FORMAT_D32_FLOAT:
            return true;
        default:
            return false;
    }
}

// Drops ALLOW_UNORDERED_ACCESS from a description whose format is depth/stencil, the one combination the console
// allows and the PC runtime refuses. Returns whether it dropped the bit. Works on D3D12_RESOURCE_DESC and
// D3D12_RESOURCE_DESC1 alike.
template <class Desc>
inline bool drop_depth_uav_flag(Desc& pc) noexcept {
    const auto flags = static_cast<std::uint32_t>(pc.Flags);
    if ((flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS) == 0 || !is_depth_format(pc.Format)) return false;
    pc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(flags & ~std::uint32_t{D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS});
    return true;
}

// Strip Xbox-only flags and translate the Xbox tiled layout. Preserve all other layouts so the host can validate them.
inline D3D12_RESOURCE_DESC pc_resource_desc(const D3D12_RESOURCE_DESC& xbox) {
    D3D12_RESOURCE_DESC pc = xbox;
    if (pc.Layout == kXboxTiledResourceLayout) {
        pc.Layout = pc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? D3D12_TEXTURE_LAYOUT_ROW_MAJOR
                                                                   : D3D12_TEXTURE_LAYOUT_UNKNOWN;
    }
    pc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(static_cast<std::uint32_t>(xbox.Flags) & kPcResourceFlagsMask);
    drop_depth_uav_flag(pc);
    return pc;
}

}  // namespace rt::d3d12_xs
