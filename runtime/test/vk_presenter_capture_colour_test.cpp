#include "vk_presenter_capture_colour.h"

#include <array>
#include <cstdio>
#include <initializer_list>
#include <vector>

#define EXPECT(condition) do { \
    if (!(condition)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #condition); return 1; } \
} while (0)

int main() {
    constexpr std::uint32_t width = 5, height = 3;
    for (const bool bgra : {false, true}) {
        const std::array<std::uint8_t, 4> clear = bgra ? std::array<std::uint8_t, 4>{230, 153, 51, 255}
                                                      : std::array<std::uint8_t, 4>{51, 153, 230, 255};
        std::vector<std::uint8_t> good;
        for (std::uint32_t i = 0; i < width * height; ++i)
            good.insert(good.end(), clear.begin(), clear.end());
        EXPECT(presenter_capture_matches_clear(good, width, height, bgra));

        auto stale_edge = good;
        for (std::uint32_t y = 0; y < height; ++y) {
            const auto offset = (y * width + width - 1) * 4;
            stale_edge[offset] = stale_edge[offset + 1] = stale_edge[offset + 2] = 0;
        }
        EXPECT(!presenter_capture_matches_clear(stale_edge, width, height, bgra));

        auto drift = good;
        drift[1] += 2;
        EXPECT(presenter_capture_matches_clear(drift, width, height, bgra));
        ++drift[1];
        EXPECT(!presenter_capture_matches_clear(drift, width, height, bgra));

        EXPECT(!presenter_capture_matches_clear({}, width, height, bgra));
        EXPECT(!presenter_capture_matches_clear({}, 0, 0, bgra));
        good.pop_back();
        EXPECT(!presenter_capture_matches_clear(good, width, height, bgra));
        good.resize(good.size() - 3);
        EXPECT(!presenter_capture_matches_clear(good, width, height, bgra));
    }
    return 0;
}
