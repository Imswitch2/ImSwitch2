// The Nucleo's firmware as a fake board on a PC: nucleo_step.cpp -- the board
// layer and the core, the very code the board runs -- over a fake nucleo_hw.
// IMSWITCH_STEP/1 on stdin and stdout; a virtual clock that moves a
// microsecond each time it is read, so busy waits end; a reader thread that
// delivers stdin in packets of at most 64 bytes as the USB interrupt does,
// held off when the board has no room, as USB holds the host off.
//
// Test hooks, outside the protocol, answered here and never seen by the
// board: a line that starts with '#'
//   #WRITES?   OK,#WRITES,<dac0>,<dac1>,<ttl0>,<ttl1>,<ttl2>: the writes that
//              reached each output since the last #RESET
//   #CHANGES?  OK,#CHANGES,<dac0>,<dac1>,<ttl0>,<ttl1>,<ttl2>: the writes that
//              changed it (a glitch is two extra changes: a window over many
//              steps must be one on and one off)
//   #RESET     OK,#RESET: the counts back to 0
//
// Build: make (in this folder), or build_msvc.cmd on Windows.
// Run:   ./nucleo_host
//
// Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.

#include "nucleo_hw.h"
#include "nucleo_step.h"

#include <atomic>
#include <mutex>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <thread>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#define READ_STDIN(buffer, size) _read(0, (buffer), (unsigned)(size))
#else
#include <unistd.h>
#define READ_STDIN(buffer, size) read(0, (buffer), (size))
#endif

namespace {

std::mutex out;                         // stdout: the board's lines and the hooks' answers
std::atomic<uint64_t> clockMicros(0);
bool masked = false;                    // the USB interrupt (main loop only)
bool completionDue = false;             // a transfer the "interrupt" has yet to complete
std::atomic<bool> rearmed(true);        // the board has room for another packet
std::atomic<unsigned long> writes[5];   // dac0, dac1, ttl0, ttl1, ttl2
std::atomic<unsigned long> changes[5];
unsigned lastValue[5] = {0, 0, 0, 0, 0};   // the outputs as the "pins" stand (main loop only)

void output(int index, unsigned value) {
    ++writes[index];
    if (value != lastValue[index]) {
        ++changes[index];
        lastValue[index] = value;
    }
}

std::string counts(const char* name, std::atomic<unsigned long>* of) {
    char text[160];
    snprintf(text, sizeof text, "OK,#%s,%lu,%lu,%lu,%lu,%lu", name, of[0].load(),
             of[1].load(), of[2].load(), of[3].load(), of[4].load());
    return text;
}

void deliverCompletion() {
    if (completionDue && !masked) {
        completionDue = false;
        nucleo_step_usb_sent();
    }
}

void answer(const std::string& line) {
    std::lock_guard<std::mutex> lock(out);
    fputs(line.c_str(), stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

void hook(const std::string& line) {
    if (line == "#WRITES?") {
        answer(counts("WRITES", writes));
    } else if (line == "#CHANGES?") {
        answer(counts("CHANGES", changes));
    } else if (line == "#RESET") {
        for (auto& w : writes) w.store(0);
        for (auto& c : changes) c.store(0);
        answer("OK,#RESET");
    } else {
        answer("ERR,syntax,not a hook of the fake Nucleo");
    }
}

void deliver(const uint8_t* data, size_t length) {
    // As the USB interrupt: a packet at a time, and none while the board has
    // not re-armed reception.
    while (length > 0) {
        while (!rearmed.load()) std::this_thread::yield();
        size_t n = length < 64 ? length : 64;
        if (!nucleo_step_usb_received(data, (uint32_t)n)) rearmed.store(false);
        data += n;
        length -= n;
    }
}

void reader() {
    // Lines that start with '#' are the hooks'; every other byte goes to the
    // board, as it comes.
    bool lineStart = true, inHook = false;
    std::string hookLine;
    std::string pending;
    char buffer[4096];
    while (true) {
        int n = (int)READ_STDIN(buffer, sizeof buffer);
        if (n <= 0) exit(0);            // the host closed the link
        for (int i = 0; i < n; ++i) {
            char c = buffer[i];
            if (lineStart && c == '#') inHook = true;
            lineStart = c == '\n';
            if (inHook) {
                if (c == '\n') {
                    if (!hookLine.empty() && hookLine.back() == '\r') hookLine.pop_back();
                    deliver((const uint8_t*)pending.data(), pending.size());
                    pending.clear();
                    hook(hookLine);
                    hookLine.clear();
                    inHook = false;
                } else {
                    hookLine += c;
                }
                continue;
            }
            pending += c;
        }
        deliver((const uint8_t*)pending.data(), pending.size());
        pending.clear();
    }
}

}  // namespace

extern "C" void hw_init(void) {}

extern "C" uint64_t hw_micros(void) {
    // A transfer completes at the next look at the clock, as the USB
    // interrupt completes one a little after it started.
    deliverCompletion();
    return ++clockMicros;
}

extern "C" void hw_dac(uint8_t channel, uint16_t code) {
    if (channel < 2) output(channel, code);
}

extern "C" void hw_ttl(uint8_t line, uint8_t level) {
    if (line < 3) output(2 + line, level ? 1u : 0u);
}

extern "C" int hw_usb_send(const uint8_t* data, uint16_t length) {
    if (completionDue) return -1;       // one transfer at a time, as CDC
    {
        std::lock_guard<std::mutex> lock(out);
        fwrite(data, 1, length, stdout);
        fflush(stdout);
    }
    completionDue = true;
    return 0;
}

extern "C" void hw_usb_irq(int enable) {
    masked = !enable;
    if (enable) deliverCompletion();
}

extern "C" void hw_usb_rearm(void) { rearmed.store(true); }

extern "C" void hw_idle(void) {
    deliverCompletion();
    std::this_thread::yield();
}

// The virtual clock runs on with every look, so every output is "late": the
// LED means nothing here.
extern "C" void hw_late_led(int on) { (void)on; }

int main() {
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    for (auto& w : writes) w.store(0);
    for (auto& c : changes) c.store(0);
    std::thread(reader).detach();
    nucleo_step_main();
    return 0;
}
