// The command a replay was preparing when it stopped, for LEONIDA_LOG_UNSUPPORTED: a list that fails with
// ReplayResult::unsupported names the command that could not be replayed. Diagnostic only.
#pragma once

#include <cstddef>
#include <cstdint>

namespace leonida {

inline std::size_t& prepare_command_index() {
    static std::size_t index = 0;
    return index;
}

inline std::uint32_t& prepare_command_kind() {
    static std::uint32_t kind = 0;
    return kind;
}

// The variant's alternative order (src/command_list.h, `using Command = std::variant<...>`).
inline const char* command_kind_name(std::uint32_t kind) {
    static const char* const names[] = {
        "TransitionBarrier",   "UavBarrier",         "AliasingBarrier",      "GlobalBarrier",
        "BufferBarrier",       "TextureBarrier",     "ClearRenderTarget",    "ClearDepthStencil",
        "CopyBuffer",          "CopyTexture",        "CopyResource",         "ResolveSubresource",
        "Predication",         "ClearUnorderedAccess", "DiscardResource",    "WriteImmediate",
        "AtomicCopyBuffer",    "BeginQuery",         "EndQuery",             "ResolveQueries",
        "DebugMarker",         "ExecuteIndirect",    "BuildAccelerationStructure", "Dispatch",
        "Draw",                "CopyAccelerationStructure", "AccelerationStructurePostbuild"};
    return kind < sizeof(names) / sizeof(names[0]) ? names[kind] : "unknown command";
}

}  // namespace leonida
