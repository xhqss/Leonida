// The GPU address arena's host mapping, published by the layer for diagnostics: LEONIDA_DUMP_TABLE shows the bytes a
// root descriptor table holds (the shaders read those bytes and hash them), without needing the GPU. Null until the
// device has created the arena, and only read when the variable is set.
#pragma once

#include <cstdint>

namespace leonida {

inline std::uint8_t* arena_host_base = nullptr;
inline std::uint64_t arena_host_size = 0;

}  // namespace leonida
