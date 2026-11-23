#pragma once

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace leonida {

// Cross-queue Wait-before-Signal (docs section 4.4, "Wait before Signal").
//
// A queue replays its ExecuteCommandLists against one device-wide layout tracker, in the order the calls reach the
// layer. When queue B calls Wait(fence, v) and the Signal(fence, v) on queue A is only submitted afterwards, B's
// lists would be replayed against the layouts from before A's work. So a queue holds a batch (and everything it
// is asked to do after it) while any fence it waits on has no submitted Signal of at least that value, and
// releases it, in order, once one has been submitted: the layouts then follow the dependency order, while Vulkan's
// own timeline semaphores still order the GPU work.
//
// This class is the device-wide half: for every fence, the highest value a Signal has been submitted for (or the
// CPU has set it to), which is not the completed value; and the held queues, which it asks to release whatever
// became ready. It owns no Vulkan objects. Semaphores it was never told about count as satisfied, so a queue used
// with a plain VkSemaphore behaves as before.
//
// Threads. Nothing here blocks on the GPU. The thread that makes a wait satisfiable (a queue's Signal, a CPU
// Signal) releases the ready batches itself, after it has released its own queue's lock: recording a held batch
// costs what its own ExecuteCommandLists would have. Only one thread releases at a time; a thread that finds
// another one releasing leaves a note and that one takes another pass, so no caller waits for another's recording.
template<class Semaphore>
class QueueHolds {
public:
    // A queue that can hold batches. Both calls take the queue's own lock; neither is called with it held.
    struct Holder {
        // Releases the held operations at the front of the queue whose waits are satisfied. True if any was.
        virtual bool release_ready() = 0;
        // Drops the held operations that wait on `semaphore` (its fence is gone), reporting each.
        virtual void discard_waiting_on(Semaphore semaphore) = 0;

    protected:
        ~Holder() = default;
    };

    void add_fence(Semaphore semaphore, std::uint64_t initial) {
        std::lock_guard lock(mutex_);
        fences_[semaphore] = initial;
    }

    // The fence is released: held batches waiting on it can never run, so they are dropped and reported.
    void remove_fence(Semaphore semaphore) {
        {
            std::lock_guard lock(mutex_);
            fences_.erase(semaphore);
        }
        if (held_.load(std::memory_order_acquire) == 0) return;
        {
            std::lock_guard pump(pump_mutex_);
            for (Holder* holder : snapshot()) holder->discard_waiting_on(semaphore);
        }
        pump_ready();
    }

    // A Signal of `value` was submitted to the GPU (or the CPU set the fence). Call after the submission, with no
    // queue lock held; then pump_ready() to release what it satisfied.
    void signaled(Semaphore semaphore, std::uint64_t value) {
        std::lock_guard lock(mutex_);
        const auto it = fences_.find(semaphore);
        if (it != fences_.end() && it->second < value) it->second = value;
    }

    // true when no submitted Signal reaches `value` yet. Unknown semaphores are satisfied.
    bool pending(Semaphore semaphore, std::uint64_t value) const {
        std::lock_guard lock(mutex_);
        const auto it = fences_.find(semaphore);
        return it != fences_.end() && it->second < value;
    }

    void attach(Holder* holder) {
        std::lock_guard lock(mutex_);
        holders_.push_back(holder);
    }

    // Returns once no thread is using `holder`.
    void detach(Holder* holder) {
        {
            std::lock_guard pump(pump_mutex_);
            std::lock_guard lock(mutex_);
            for (auto it = holders_.begin(); it != holders_.end(); ++it)
                if (*it == holder) {
                    holders_.erase(it);
                    break;
                }
        }
        pump_ready();
    }

    // Held operations over all queues, for the cheap "is there anything to do" test.
    void held_changed(std::int64_t delta) { held_.fetch_add(delta, std::memory_order_acq_rel); }
    bool any_held() const { return held_.load(std::memory_order_acquire) > 0; }

    // Releases every held operation that has become ready, in dependency order: a release can submit a Signal
    // that makes another queue's front ready, so passes repeat until one finds nothing new.
    void pump_ready() {
        if (!any_held()) return;
        dirty_.store(true, std::memory_order_release);
        if (in_pump()) return;  // a release on this thread submitted a Signal: the running pass will go again
        for (;;) {
            std::unique_lock pump(pump_mutex_, std::try_to_lock);
            if (!pump) return;  // another thread is releasing and will see dirty_
            in_pump() = true;
            while (dirty_.exchange(false, std::memory_order_acq_rel)) {
                bool progress = true;
                while (progress) {
                    progress = false;
                    for (Holder* holder : snapshot()) progress |= holder->release_ready();
                }
            }
            in_pump() = false;
            pump.unlock();
            if (!dirty_.load(std::memory_order_acquire)) return;
        }
    }

private:
    static bool& in_pump() {
        static thread_local bool pumping = false;
        return pumping;
    }

    std::vector<Holder*> snapshot() const {
        std::lock_guard lock(mutex_);
        return holders_;
    }

    mutable std::mutex mutex_;  // leaf: fences_ and holders_
    std::mutex pump_mutex_;     // one releasing thread; held across release_ready(), so detach() waits for it
    std::unordered_map<Semaphore, std::uint64_t> fences_;
    std::vector<Holder*> holders_;
    std::atomic<std::int64_t> held_{0};
    std::atomic<bool> dirty_{false};
};

}  // namespace leonida
