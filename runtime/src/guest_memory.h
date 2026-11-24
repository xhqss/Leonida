#pragma once

// Reading title memory that the title uses as GPU memory (see VulkanContext::guest_buffer): the title's allocator
// hands out pages in this process, so the bytes can be copied out of them directly, but only the pages the title has
// committed. Grand Theft Auto VI's interface vertices are such addresses, and a view can run past the end of the
// allocation it starts in, so a copy has to stop at the first page it cannot read instead of faulting.

#include <windows.h>

#include <algorithm>
#include <cstdint>
#include <cstring>

namespace leonida {

// Whether `address` is committed, readable title memory. A GPU address the layer handed out is not: it is not a host
// pointer, so a title address that is one can be told apart from an address that already is a device address.
inline bool is_committed(const void* address) {
    MEMORY_BASIC_INFORMATION info{};
    if (VirtualQuery(address, &info, sizeof(info)) != sizeof(info)) return false;
    return info.State == MEM_COMMIT && (info.Protect & (PAGE_NOACCESS | PAGE_GUARD)) == 0;
}

// Copies [address, address + bytes) out of title memory into `to`. Pages that are not committed (or are guarded) stop
// the copy there: the destination keeps what it had, which is what a GPU read of unmapped memory shows a reader.
inline void copy_committed(std::uint8_t* to, std::uint64_t address, std::uint64_t bytes) {
    std::uint64_t at = address;
    const std::uint64_t end = address + bytes;
    while (at < end) {
        MEMORY_BASIC_INFORMATION info{};
        if (VirtualQuery(reinterpret_cast<const void*>(at), &info, sizeof(info)) != sizeof(info)) return;
        if (info.State != MEM_COMMIT || (info.Protect & (PAGE_NOACCESS | PAGE_GUARD)) != 0) return;
        const auto region_end = reinterpret_cast<std::uint64_t>(info.BaseAddress) + info.RegionSize;
        const std::uint64_t stop = std::min<std::uint64_t>(end, region_end);
        if (stop <= at) return;  // a region that does not advance: nothing more can be copied safely
        std::memcpy(to + (at - address), reinterpret_cast<const void*>(at), static_cast<std::size_t>(stop - at));
        at = stop;
    }
}

// Writes `bytes` into title memory at `address`, the way a GPU write into the console's unified memory would land.
// A page the title has not committed, or one that is not writable, stops the write there: past such a page there is
// nothing a reader could observe, and writing through it would fault instead.
inline void write_committed(std::uint64_t address, const std::uint8_t* from, std::uint64_t bytes) {
    std::uint64_t at = address;
    const std::uint64_t end = address + bytes;
    while (at < end) {
        MEMORY_BASIC_INFORMATION info{};
        if (VirtualQuery(reinterpret_cast<const void*>(at), &info, sizeof(info)) != sizeof(info)) return;
        if (info.State != MEM_COMMIT || (info.Protect & (PAGE_NOACCESS | PAGE_GUARD)) != 0) return;
        // PAGE_READWRITE / PAGE_WRITECOPY / PAGE_EXECUTE_READWRITE / PAGE_EXECUTE_WRITECOPY are the writable ones.
        if ((info.Protect & 0xCCu) == 0) return;
        const auto region_end = reinterpret_cast<std::uint64_t>(info.BaseAddress) + info.RegionSize;
        const std::uint64_t stop = std::min<std::uint64_t>(end, region_end);
        if (stop <= at) return;  // a region that does not advance: nothing more can be written safely
        std::memcpy(reinterpret_cast<void*>(at), from + (at - address), static_cast<std::size_t>(stop - at));
        at = stop;
    }
}

}  // namespace leonida
