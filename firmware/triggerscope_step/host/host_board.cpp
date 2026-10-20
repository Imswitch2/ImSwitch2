// The firmware core as a fake board on a PC: IMSWITCH_STEP/1 on stdin and
// stdout, a virtual clock that jumps instead of waiting, outputs that go
// nowhere. The conformance runner (tools/step_board_conformance.py) and the
// unit tests drive it over a pipe; what it logs must match the interpreter
// exactly as the simulated board's log does.
//
// Test hooks, outside the protocol (the simulated board's arrivals and
// fail_at_step, which a board cannot be told over the wire): between runs,
//   #ARRIVE,<t1>;<t2>;...   the trigger input pulses at these ticks since RUN
//   #FAIL,<step>            the next run fails at that step
//   #FAIL,-                 no injected failure
// each answered OK,#ARRIVE or OK,#FAIL.
//
// Build: make (in this folder). Run: ./host_board [--grid-ns N]
//
// Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.

#include "../TriggerScopeStep/step_core.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/select.h>

using namespace stepfw;

static const uint32_t LOG_CAPACITY = 1u << 18;

class VirtualHal : public Hal {
public:
    VirtualHal() : now_(0), arrivalCount_(0), runStart_(0) {
        memset(&output_, 0, sizeof output_);
    }

    uint64_t now() override { return now_; }

    void sleepUntil(uint64_t t) override {
        if (t > now_) now_ = t;
    }

    Arrival waitArrival(uint64_t start, bool hasDeadline, uint64_t deadline,
                        uint64_t* at) override {
        // The arrivals were chosen before the run, in ticks since RUN; the
        // run's clock started at the board's now() when RUN was answered.
        for (int i = 0; i < arrivalCount_; ++i) {
            uint64_t abs = runStart_ + arrivals_[i];
            if (abs < start) continue;
            if (hasDeadline && abs > deadline) break;
            *at = abs;
            if (abs > now_) now_ = abs;
            return ARRIVED;
        }
        if (hasDeadline) {
            if (deadline > now_) now_ = deadline;
            return TIMED_OUT;
        }
        // Nothing is coming and nothing is waited out: the board waits for
        // a STOP, which the core reads once this returns. Block until the
        // link has something, so the process does not spin.
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(STDIN_FILENO, &fds);
        select(STDIN_FILENO + 1, &fds, NULL, NULL, NULL);
        return SLICE;
    }

    int readByte() override {
        // Non-blocking between runs too: the main loop blocks on select.
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(STDIN_FILENO, &fds);
        struct timeval zero = {0, 0};
        if (select(STDIN_FILENO + 1, &fds, NULL, NULL, &zero) <= 0) return -1;
        unsigned char c;
        ssize_t n = read(STDIN_FILENO, &c, 1);
        if (n <= 0) {
            if (n == 0) exit(0);     // the host closed the link
            return -1;
        }
        return c;
    }

    void writeLine(const char* line) override {
        fputs(line, stdout);
        fputc('\n', stdout);
        fflush(stdout);
    }

    void writeDac(uint8_t index, double volts) override { output_.dac[index] = volts; }
    void writeTtl(uint8_t index, uint8_t level) override { output_.ttl[index] = level; }

    // The hooks.
    void setArrivals(const char* list) {
        arrivalCount_ = 0;
        if (strcmp(list, "-") == 0 || *list == 0) return;
        char buffer[1024];
        strncpy(buffer, list, sizeof buffer - 1);
        buffer[sizeof buffer - 1] = 0;
        char* item = buffer;
        while (item != NULL && arrivalCount_ < 64) {
            char* semi = strchr(item, ';');
            if (semi) *semi = 0;
            arrivals_[arrivalCount_++] = strtoull(item, NULL, 10);
            item = semi ? semi + 1 : NULL;
        }
    }
    void markRunStart() { runStart_ = now_; }

private:
    uint64_t now_;
    uint64_t arrivals_[64];
    int arrivalCount_;
    uint64_t runStart_;
    struct { double dac[MAX_DACS]; uint8_t ttl[MAX_TTLS]; } output_;
};

static BoardInfo info() {
    BoardInfo b;
    memset(&b, 0, sizeof b);
    b.board = "host";
    b.firmware = "fake";
    b.gridNs = 1000;
    b.dacCount = 2;
    b.dacLow[0] = -10.0; b.dacHigh[0] = 10.0; b.dacBits[0] = 16;
    b.dacLow[1] = 0.0; b.dacHigh[1] = 10.0; b.dacBits[1] = 16;
    b.ttlCount = 4;
    b.triggerIn = true;
    b.hasLatency = true;
    b.latencyTicks = 2;
    b.hasAlive = true;
    b.aliveTicks = 500000;
    b.maxSteps = 0xFFFFFFFFu;
    b.stepReportTicks = 100000;
    return b;
}

int main(int argc, char** argv) {
    static BoardInfo boardInfo = info();
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--grid-ns") == 0 && i + 1 < argc) boardInfo.gridNs = strtoul(argv[++i], NULL, 10);
        else if (strcmp(argv[i], "--no-latency") == 0) boardInfo.hasLatency = false;
    }
    static LogEntry entries[LOG_CAPACITY];
    static Log log(entries, LOG_CAPACITY);
    static VirtualHal hal;
    static Board board(hal, boardInfo, log);

    // The hooks are read here, before the core sees a line; everything else
    // is the core's. A RUN is noted so the hooks' ticks are since RUN.
    char line[LINE_MAX + 2];
    size_t length = 0;
    bool hook = false;
    while (true) {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(STDIN_FILENO, &fds);
        if (select(STDIN_FILENO + 1, &fds, NULL, NULL, NULL) <= 0) continue;
        unsigned char c;
        ssize_t n = read(STDIN_FILENO, &c, 1);
        if (n <= 0) break;
        if (length == 0 && c == '#') hook = true;
        if (hook) {
            if (c == '\n') {
                line[length] = 0;
                if (length && line[length - 1] == '\r') line[length - 1] = 0;
                if (strncmp(line, "#ARRIVE,", 8) == 0) {
                    hal.setArrivals(line + 8);
                    hal.writeLine("OK,#ARRIVE");
                } else if (strncmp(line, "#FAIL,", 6) == 0) {
                    if (strcmp(line + 6, "-") == 0) board.failAt(false, 0);
                    else board.failAt(true, (uint32_t)strtoul(line + 6, NULL, 10));
                    hal.writeLine("OK,#FAIL");
                } else {
                    hal.writeLine("ERR,syntax,not a hook of the fake board");
                }
                length = 0;
                hook = false;
            } else if (length < LINE_MAX) {
                line[length++] = (char)c;
            }
            continue;
        }
        if (c == '\n' && length == 3 && strncmp(line, "RUN", 3) == 0) hal.markRunStart();
        if (c == '\n') length = 0;
        else if (length < LINE_MAX) line[length++] = (char)c;
        board.feed(c);
    }
    return 0;
}
