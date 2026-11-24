// A pipeline handle to a one-line note (the translated stages and the digest of the module bytes each was built
// from), for LEONIDA_LOG_DRAW_PIPELINE: a draw that paints nothing can be checked against the pipeline it really
// ran and the modules that pipeline was built from. Diagnostic only; the map stays empty unless the variable is set.
#pragma once

#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>

namespace leonida {

inline std::mutex& pipeline_note_mutex() {
    static std::mutex mutex;
    return mutex;
}

inline std::unordered_map<std::uintptr_t, std::string>& pipeline_notes() {
    static std::unordered_map<std::uintptr_t, std::string> notes;
    return notes;
}

inline void set_pipeline_note(std::uintptr_t pipeline, std::string note) {
    std::lock_guard lock(pipeline_note_mutex());
    pipeline_notes()[pipeline] = std::move(note);
}

inline std::string pipeline_note(std::uintptr_t pipeline) {
    std::lock_guard lock(pipeline_note_mutex());
    const auto it = pipeline_notes().find(pipeline);
    return it == pipeline_notes().end() ? std::string() : it->second;
}

inline void forget_pipeline_note(std::uintptr_t pipeline) {
    std::lock_guard lock(pipeline_note_mutex());
    pipeline_notes().erase(pipeline);
}

}  // namespace leonida
