// LEONIDA_THREAD_MAP=<seconds>: where this process's threads are, from inside the process.
//
// The title burns more than a dozen cores while nothing happens on the GPU (docs/
// 2026-11-25_title_spins_on_pipeline_state.md section 15.3), and the outside-in tools cannot say whose code is
// spinning: a thread-map script reading another process's contexts got rip=unreadable for every thread. A thread can
// always read its own process's contexts and stacks, so the sample runs here instead: suspend each other thread for one
// GetThreadContext, walk its stack with RtlVirtualUnwind (the loader registers every mapped image's .pdata, so console
// code unwinds), and take each thread's CPU time from GetThreadTimes so a spin shows up as a percentage.
#pragma once

#include <cstdint>
#include <string>

namespace rt::thread_sampler {

// Resolves an address to a mapped console image and an RVA. The loader's find_address, which is safe from any thread;
// null leaves only the host's own modules resolvable.
using ImageResolver = bool (*)(std::uint64_t address, std::string& module, std::uint32_t& rva);

void set_image_resolver(ImageResolver resolver);

// Starts the sampler thread when LEONIDA_THREAD_MAP is set; a no-op otherwise. Safe to call once, early.
void install();

}  // namespace rt::thread_sampler
