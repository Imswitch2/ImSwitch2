// The NUCLEO-H7A3ZI-Q as an IMSWITCH_STEP/1 step board: the polarization
// rig's timing board, with galvo X and Y on its two DACs and the 405, 488 and
// camera lines on PA0-PA2. The protocol, the program and the executor are the
// core (firmware/triggerscope_step/TriggerScopeStep/step_core.{h,cpp}), the
// same code the TriggerScope runs; this file is the board it runs on: the
// clock, the USB link's two queues, the outputs, what the board declares.
//
// The hardware is behind nucleo_hw.h -- nucleo_hw_stm32.c on the board,
// ../host/nucleo_host.cpp on a PC -- so this file runs, and is tested, on a PC
// too (imswitch/imcontrol/_test/unit/test_nucleo_firmware_host.py).
//
// The core is built here, in this one translation unit, with this board's
// capacities (STEPFW_*). Do not also compile step_core.cpp on its own: it is
// on the include path only (../README.md).
//
// What the board declares, and why (step-engine-protocol.rst, *CAPS?):
// - grid_ns 1000: the clock is the core's cycle counter in microseconds.
// - dac 0 and 1, 0 to 3.3 V, 12 bits: DAC1's two channels, written directly.
// - ttl 3: PA0-PA2, written through the port's set/reset register.
// - no trigger input: none is wired (a free pin can be one later).
// - alive_ticks 500000 (0.5 s), as the TriggerScope.
//
// Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.

// The Galvo Scan is a table of ~1,100 positions (a TABLE line of ~25 kB) with
// windows of ~250 steps each (as many phases per event): far past the
// TriggerScope's defaults. These fit the H7A3's 1 MB of AXI RAM with the log.
#define STEPFW_TABLE_POOL 8192
#define STEPFW_MAX_PHASES 1024
#define STEPFW_LINE_MAX 49152

#include "step_core.h"
#include "step_core.cpp"

#include <atomic>
#include <math.h>
#include <string.h>

#include "nucleo_hw.h"
#include "nucleo_step.h"

namespace {

const char* const FIRMWARE_VERSION = "nucleo-step-0.1";

// A busy wait returns this often, so the core can read the link (a STOP) and
// send its heartbeat: 1 ms, as the TriggerScope.
const uint32_t SLICE_TICKS = 1000;

const uint32_t LOG_CAPACITY = 8192;
const double DAC_LOW = 0.0, DAC_HIGH = 3.3;
const long DAC_TOP = 4095;

// An output written later than this after its time lights the red LED until
// the next run. A step's opening outputs are always a little late (written
// once the core has prepared the step, a few microseconds); later than this
// and the board is falling behind its steps -- make them longer.
const uint64_t LATE_TICKS = 10;

// -- the link in: bytes from the USB interrupt to the main loop -----------------------

const uint32_t RX_SIZE = 8192;              // a power of two
const uint32_t RX_PACKET = 64;              // a full-speed packet: the most one delivery brings
uint8_t rxBuffer[RX_SIZE];
std::atomic<uint32_t> rxHead(0);            // the interrupt's; free-running
std::atomic<uint32_t> rxTail(0);            // the main loop's; free-running
std::atomic<bool> rxHeld(false);            // reception not re-armed: the host waits

void rxRearmIfRoom() {
    if (!rxHeld.load(std::memory_order_acquire)) return;
    uint32_t used = rxHead.load(std::memory_order_acquire) - rxTail.load(std::memory_order_relaxed);
    if (RX_SIZE - used < RX_PACKET) return;
    rxHeld.store(false, std::memory_order_release);
    hw_usb_rearm();
}

int rxRead() {
    uint32_t tail = rxTail.load(std::memory_order_relaxed);
    if (rxHead.load(std::memory_order_acquire) == tail) {
        rxRearmIfRoom();
        return -1;
    }
    int byte = rxBuffer[tail & (RX_SIZE - 1)];
    rxTail.store(tail + 1, std::memory_order_release);
    rxRearmIfRoom();
    return byte;
}

// -- the link out: lines from the main loop, sent by the USB interrupt ----------------
//
// The main loop appends at the head; a transfer carries the bytes from the
// tail, and its completion (in the interrupt) moves the tail on and starts the
// next. Both sides start transfers, so a start happens with the USB interrupt
// masked when the main loop does it.

const uint32_t TX_SIZE = 16384;             // a power of two
const uint32_t TX_CHUNK = 2048;             // the most one transfer carries
// After this long with no room, the host is not reading: the line is dropped,
// and so is every line after it until a transfer completes again.
const uint64_t TX_PATIENCE_US = 200000;
uint8_t txBuffer[TX_SIZE];
volatile uint32_t txHead = 0;               // the main loop's; free-running
volatile uint32_t txTail = 0;               // the interrupt's; free-running
volatile uint32_t txInFlight = 0;           // what the current transfer carries; 0: none
volatile bool txStalled = false;

void txStart() {
    // With the USB interrupt masked, or from it.
    if (txInFlight != 0 || txHead == txTail) return;
    uint32_t at = txTail & (TX_SIZE - 1);
    uint32_t length = txHead - txTail;
    if (length > TX_SIZE - at) length = TX_SIZE - at;       // contiguous
    if (length > TX_CHUNK) length = TX_CHUNK;
    txInFlight = length;
    if (hw_usb_send(&txBuffer[at], (uint16_t)length) != 0) txInFlight = 0;   // later
}

void txKick() {
    hw_usb_irq(0);
    txStart();
    hw_usb_irq(1);
}

void txPut(const char* data, uint32_t length) {
    uint64_t since = hw_micros();
    while (length > 0) {
        uint32_t room = TX_SIZE - (txHead - txTail);
        if (room == 0) {
            if (txStalled) return;
            txKick();
            if (hw_micros() - since > TX_PATIENCE_US) {
                txStalled = true;
                return;
            }
            continue;
        }
        uint32_t at = txHead & (TX_SIZE - 1);
        uint32_t n = length;
        if (n > room) n = room;
        if (n > TX_SIZE - at) n = TX_SIZE - at;
        memcpy(&txBuffer[at], data, n);
        std::atomic_signal_fence(std::memory_order_release);   // the bytes before the head
        txHead = txHead + n;
        data += n;
        length -= n;
    }
}

// -- the board -------------------------------------------------------------------------

class NucleoHal : public stepfw::Hal {
public:
    uint64_t now() override { return hw_micros(); }

    void sleepUntil(uint64_t t) override {
        uint64_t start = hw_micros();
        while (true) {
            uint64_t n = hw_micros();
            if (n >= t || n - start >= SLICE_TICKS) return;
        }
    }

    Arrival waitArrival(uint64_t start, bool hasDeadline, uint64_t deadline,
                        uint64_t* at) override {
        // No trigger input is declared, so the host sends no trigger wait;
        // should one come, nothing arrives.
        (void)start;
        (void)at;
        uint64_t began = hw_micros();
        while (true) {
            uint64_t n = hw_micros();
            if (hasDeadline && n >= deadline) return TIMED_OUT;
            if (n - began >= SLICE_TICKS) return SLICE;
        }
    }

    int readByte() override { return rxRead(); }

    void late(uint64_t ticks) override {
        if (ticks > LATE_TICKS && !lateShown_) {
            lateShown_ = true;
            hw_late_led(1);
        }
    }

    void writeLine(const char* line) override {
        if (strcmp(line, "OK,RUN") == 0 && lateShown_) {   // a new run: a clean slate
            lateShown_ = false;
            hw_late_led(0);
        }
        if (txStalled && txInFlight == 0 && txHead == txTail) txStalled = false;
        txPut(line, (uint32_t)strlen(line));
        txPut("\n", 1);
        txKick();
    }

    void writeDac(uint8_t index, double volts) override {
        long code = lround((volts - DAC_LOW) / (DAC_HIGH - DAC_LOW) * (double)DAC_TOP);
        if (code < 0) code = 0;
        if (code > DAC_TOP) code = DAC_TOP;
        hw_dac(index, (uint16_t)code);
    }

    void writeTtl(uint8_t index, uint8_t level) override { hw_ttl(index, level ? 1 : 0); }

private:
    bool lateShown_ = false;
};

stepfw::BoardInfo describeBoard() {
    stepfw::BoardInfo b;
    memset(&b, 0, sizeof b);
    b.board = "NUCLEO-H7A3ZI-Q";
    b.firmware = FIRMWARE_VERSION;
    b.gridNs = 1000;
    b.dacCount = 2;
    for (uint8_t i = 0; i < 2; ++i) {
        b.dacLow[i] = DAC_LOW;
        b.dacHigh[i] = DAC_HIGH;
        b.dacBits[i] = 12;
    }
    b.ttlCount = 3;
    b.triggerIn = false;
    b.hasLatency = false;
    b.hasAlive = true;
    b.aliveTicks = 500000;
    b.maxSteps = 0xFFFFFFFFu;
    b.stepReportTicks = 100000;
    return b;
}

stepfw::LogEntry logEntries[LOG_CAPACITY];

}  // namespace

extern "C" int nucleo_step_usb_received(const uint8_t* data, uint32_t length) {
    uint32_t head = rxHead.load(std::memory_order_relaxed);
    uint32_t tail = rxTail.load(std::memory_order_acquire);
    for (uint32_t i = 0; i < length && head - tail < RX_SIZE; ++i) {
        rxBuffer[head & (RX_SIZE - 1)] = data[i];
        ++head;
    }
    rxHead.store(head, std::memory_order_release);
    if (RX_SIZE - (head - tail) >= RX_PACKET) return 1;
    rxHeld.store(true, std::memory_order_release);
    return 0;
}

extern "C" void nucleo_step_usb_sent(void) {
    txTail = txTail + txInFlight;
    txInFlight = 0;
    txStalled = false;
    txStart();
}

extern "C" void nucleo_step_main(void) {
    hw_init();
    static stepfw::BoardInfo info = describeBoard();
    static stepfw::Log log(logEntries, LOG_CAPACITY);
    static NucleoHal hal;
    static stepfw::Board board(hal, info, log);
    // No banner: the host's first line from this board is its answer to *IDN?.
    while (true) {
        int byte = hal.readByte();
        if (byte >= 0) {
            board.feed(byte);       // a RUN executes inside this call
        } else {
            txKick();               // a transfer the host was not ready for
            hw_idle();
        }
    }
}
