#include "../src/conservative_raster.h"
#include <cstdio>
#include <limits>

int main() {
    using leonida::conservative_raster::tier1;
    unsigned failures = 0;
    const auto check = [&](bool value) { if (!value) ++failures; };
    check(tier1(true, 8, 0.0f));
    check(tier1(true, 8, 1.0f / 256.0f));
    check(tier1(true, 8, 0.5f - 1.0f / 512.0f));
    check(!tier1(false, 8, 0.0f));
    check(!tier1(true, 7, 0.0f));
    check(!tier1(true, 8, 0.5f));
    check(!tier1(true, 8, -0.001f));
    check(!tier1(true, 8, std::numeric_limits<float>::infinity()));
    check(!tier1(true, 8, std::numeric_limits<float>::quiet_NaN()));
    if (failures) std::fprintf(stderr, "conservative raster capability checks: %u failures\n", failures);
    else std::puts("conservative raster: enabled extension, precision and uncertainty bounds checked");
    return failures ? 1 : 0;
}
