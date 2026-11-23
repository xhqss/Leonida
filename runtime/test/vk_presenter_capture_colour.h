#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <span>

// The capture in step 2 of vk_presenter_sync_test should contain one uniform clear colour.
inline bool presenter_capture_matches_clear(std::span<const std::uint8_t> pixels, std::uint32_t width,
                                            std::uint32_t height, bool bgra) {
    const std::size_t pixel_count = pixels.size() / 4;
    if (width == 0 || height == 0 || pixels.size() % 4 != 0 ||
        pixel_count / width != height || pixel_count % width != 0)
        return false;

    for (std::size_t i = 0; i < pixels.size(); i += 4) {
        const int r = pixels[i + (bgra ? 2 : 0)];
        const int g = pixels[i + 1];
        const int b = pixels[i + (bgra ? 0 : 2)];
        if (std::abs(r - 51) > 2 || std::abs(g - 153) > 2 || std::abs(b - 230) > 2)
            return false;
    }
    return true;
}
