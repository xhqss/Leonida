#pragma once

// Inflate for the runtime's own readers: the shader library's FSSM blobs and the DStorage shim's zlib requests
// (docs/d3d12_xs.md, "DStorage"). RFC 1950 wrapper, RFC 1951 deflate, Adler-32 checked; no third-party code and
// nothing here allocates beyond the output vector. Split out of shader_library.h so a shim can use it without the
// shader library.

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <utility>
#include <vector>

namespace leonida {

// ---------------------------------------------------------------------------------------------------------------
// zlib inflate

namespace detail {

class BitReader {
public:
    explicit BitReader(std::span<const std::uint8_t> in) : in_(in) {}

    std::uint32_t bits(int need) {
        std::uint64_t value = buffer_;
        while (count_ < need) {
            if (pos_ >= in_.size()) {
                failed_ = true;
                return 0;
            }
            value |= std::uint64_t(in_[pos_++]) << count_;
            count_ += 8;
        }
        buffer_ = value >> need;
        count_ -= need;
        return std::uint32_t(value & ((std::uint64_t(1) << need) - 1));
    }

    void align() {
        buffer_ = 0;
        count_ = 0;
    }

    bool byte(std::uint8_t& out) {
        if (pos_ >= in_.size()) return failed_ = true, false;
        out = in_[pos_++];
        return true;
    }

    std::size_t position() const { return pos_; }
    bool failed() const { return failed_; }

private:
    std::span<const std::uint8_t> in_;
    std::size_t pos_ = 0;
    std::uint64_t buffer_ = 0;
    int count_ = 0;
    bool failed_ = false;
};

struct Huffman {
    std::array<std::int16_t, 16> count{};
    std::array<std::int16_t, 288> symbol{};
};

// Canonical Huffman table from code lengths. Returns false for an over-subscribed set.
inline bool build_huffman(Huffman& h, const std::int16_t* lengths, int n) {
    h.count.fill(0);
    for (int i = 0; i < n; ++i) ++h.count[lengths[i]];
    if (h.count[0] == n) return true;  // no codes: valid only if never used
    int left = 1;
    for (int len = 1; len < 16; ++len) {
        left <<= 1;
        left -= h.count[len];
        if (left < 0) return false;
    }
    std::array<std::int16_t, 16> offsets{};
    for (int len = 1; len < 15; ++len) offsets[len + 1] = std::int16_t(offsets[len] + h.count[len]);
    for (int i = 0; i < n; ++i)
        if (lengths[i]) h.symbol[offsets[lengths[i]]++] = std::int16_t(i);
    return true;
}

inline int decode(BitReader& in, const Huffman& h) {
    int code = 0, first = 0, index = 0;
    for (int len = 1; len < 16; ++len) {
        code |= int(in.bits(1));
        const int count = h.count[len];
        if (code - count < first) return h.symbol[index + (code - first)];
        index += count;
        first += count;
        first <<= 1;
        code <<= 1;
        if (in.failed()) return -1;
    }
    return -1;
}

inline bool inflate_codes(BitReader& in, std::vector<std::uint8_t>& out, const Huffman& lengths,
                          const Huffman& distances, std::size_t limit) {
    static constexpr std::int16_t length_base[29] = {3,  4,  5,  6,  7,  8,  9,  10, 11,  13,  15,  17,  19,  23, 27,
                                                     31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258};
    static constexpr std::int16_t length_extra[29] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2,
                                                      2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0};
    static constexpr std::int16_t distance_base[30] = {1,    2,    3,    4,    5,    7,     9,     13,    17,  25,
                                                       33,   49,   65,   97,   129,  193,   257,   385,   513, 769,
                                                       1025, 1537, 2049, 3073, 4097, 6145,  8193,  12289, 16385, 24577};
    static constexpr std::int16_t distance_extra[30] = {0, 0, 0, 0, 1, 1, 2, 2,  3,  3,  4,  4,  5,  5,  6,
                                                        6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13};
    for (;;) {
        int symbol = decode(in, lengths);
        if (symbol < 0 || in.failed()) return false;
        if (symbol < 256) {
            if (out.size() >= limit) return false;
            out.push_back(std::uint8_t(symbol));
            continue;
        }
        if (symbol == 256) return true;
        symbol -= 257;
        if (symbol >= 29) return false;
        const std::size_t length = std::size_t(length_base[symbol]) + in.bits(length_extra[symbol]);
        const int d = decode(in, distances);
        if (d < 0 || d >= 30) return false;
        const std::size_t distance = std::size_t(distance_base[d]) + in.bits(distance_extra[d]);
        if (in.failed() || distance > out.size() || out.size() + length > limit) return false;
        const std::size_t from = out.size() - distance;
        for (std::size_t i = 0; i < length; ++i) out.push_back(out[from + i]);
    }
}

} // namespace detail

// Inflates a zlib stream (RFC 1950: header, deflate data, Adler-32). Returns false for a malformed stream, a
// preset dictionary, a checksum mismatch, or output beyond `limit` bytes.
inline bool zlib_inflate(std::span<const std::uint8_t> in, std::vector<std::uint8_t>& out,
                         std::size_t limit = std::size_t(1) << 30) {
    out.clear();
    if (in.size() < 6) return false;
    const unsigned cmf = in[0], flg = in[1];
    if ((cmf & 0x0f) != 8 || (cmf >> 4) > 7 || ((cmf << 8) | flg) % 31 != 0 || (flg & 0x20)) return false;
    detail::BitReader reader(in.subspan(2));
    for (bool last = false; !last;) {
        last = reader.bits(1) != 0;
        const std::uint32_t type = reader.bits(2);
        if (reader.failed()) return false;
        if (type == 0) {  // stored
            reader.align();
            std::uint8_t b[4];
            for (auto& x : b)
                if (!reader.byte(x)) return false;
            const unsigned length = b[0] | (b[1] << 8), complement = b[2] | (b[3] << 8);
            if (length != (~complement & 0xffff) || out.size() + length > limit) return false;
            for (unsigned i = 0; i < length; ++i) {
                std::uint8_t x;
                if (!reader.byte(x)) return false;
                out.push_back(x);
            }
        } else if (type == 1) {  // fixed Huffman
            static const auto tables = [] {
                std::pair<detail::Huffman, detail::Huffman> t;
                std::int16_t lengths[288];
                for (int i = 0; i < 144; ++i) lengths[i] = 8;
                for (int i = 144; i < 256; ++i) lengths[i] = 9;
                for (int i = 256; i < 280; ++i) lengths[i] = 7;
                for (int i = 280; i < 288; ++i) lengths[i] = 8;
                detail::build_huffman(t.first, lengths, 288);
                for (int i = 0; i < 30; ++i) lengths[i] = 5;
                detail::build_huffman(t.second, lengths, 30);
                return t;
            }();
            if (!detail::inflate_codes(reader, out, tables.first, tables.second, limit)) return false;
        } else if (type == 2) {  // dynamic Huffman
            const int literal_count = int(reader.bits(5)) + 257, distance_count = int(reader.bits(5)) + 1;
            const int code_count = int(reader.bits(4)) + 4;
            if (literal_count > 286 || distance_count > 30) return false;
            static constexpr int order[19] = {16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15};
            std::int16_t lengths[320] = {};
            for (int i = 0; i < code_count; ++i) lengths[order[i]] = std::int16_t(reader.bits(3));
            detail::Huffman code_lengths;
            if (!detail::build_huffman(code_lengths, lengths, 19)) return false;
            for (int i = 0; i < literal_count + distance_count;) {
                const int symbol = detail::decode(reader, code_lengths);
                if (symbol < 0) return false;
                if (symbol < 16) {
                    lengths[i++] = std::int16_t(symbol);
                    continue;
                }
                std::int16_t value = 0;
                int repeat;
                if (symbol == 16) {
                    if (i == 0) return false;
                    value = lengths[i - 1];
                    repeat = 3 + int(reader.bits(2));
                } else if (symbol == 17) {
                    repeat = 3 + int(reader.bits(3));
                } else {
                    repeat = 11 + int(reader.bits(7));
                }
                if (i + repeat > literal_count + distance_count) return false;
                while (repeat--) lengths[i++] = value;
            }
            if (lengths[256] == 0) return false;  // no end-of-block code
            detail::Huffman literal, distance;
            if (!detail::build_huffman(literal, lengths, literal_count) ||
                !detail::build_huffman(distance, lengths + literal_count, distance_count))
                return false;
            if (!detail::inflate_codes(reader, out, literal, distance, limit)) return false;
        } else {
            return false;
        }
        if (reader.failed()) return false;
    }
    // Adler-32, big-endian, after the deflate data's last whole byte.
    const std::size_t at = 2 + reader.position();
    if (at + 4 > in.size()) return false;
    std::uint32_t a = 1, b = 0;
    for (const std::uint8_t x : out) {
        a = (a + x) % 65521;
        b = (b + a) % 65521;
    }
    const std::uint32_t expected = (std::uint32_t(in[at]) << 24) | (std::uint32_t(in[at + 1]) << 16) |
                                   (std::uint32_t(in[at + 2]) << 8) | in[at + 3];
    return ((b << 16) | a) == expected;
}

}  // namespace leonida
