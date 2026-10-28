/*
  TriggerScope 16 R4 as an IMSWITCH_STEP/1 step board.

  The protocol, the program and the executor are in step_core.{h,cpp}
  (platform independent, tested on a PC). This sketch is the board: the
  LTC2668 16-channel DAC over SPI, the TTL lines (0-8 on Teensy pins, 9-15
  through the MCP23017 expander), the trigger input, the USB serial link
  and the clock -- all as the lab's TriggerSwitch_0.1 sketch drove them,
  ported here without its scan routines. docs/devices/triggerscope-firmware.rst
  says how to build, flash and test it.

  What the board declares, and why (step-engine-protocol.rst, *CAPS?):
  - grid_ns 1000: the Teensy's microsecond clock.
  - dac 0..15, -10 to 10 V, 16 bits: the LTC2668-16 at the +-10 V span the
    old sketch set for every channel (initDACRange 3).
  - ttl 16: lines 9-15 switch over I2C and take far longer than the grid
    (unmeasured, a few hundred microseconds); keep timed pulses on 0-8.
  - trigger_in: pin 0, released on a rising edge.
  - trigger_latency_ticks: NOT declared until measured (set
    TRIGGER_LATENCY_TICKS below after measuring it); the host refuses
    trigger waits as unknown rather than guess it.
  - alive_ticks 500000 (0.5 s): a message at least this often while a
    program runs, so the host can tell a hung board from a busy one well
    inside its own reply timeout.

  Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.
  The DAC and expander code derives from ARC's TriggerScope sketch (c) 2015-2018
  Advanced Research Consulting, as modified by Andreas Boden, under its terms.
*/

#include <SPI.h>
#include <Wire.h>
#include "Adafruit_MCP23017.h"
#include "step_core.h"

// #define TRIGGER_LATENCY_TICKS 0   // declare once measured (see the docs)

static const char* const FIRMWARE_VERSION = "step-0.1";

// -- pins, from TriggerSwitch_0.1 ------------------------------------------------
static const uint8_t DAC_CS = 10;
static const uint8_t DAC_CLR = 9;
static const uint8_t TRIGGER_PIN = 0;
static const uint8_t MCP_ADDRESS = 0x27;
static const uint8_t PWR_LED = 11, DAC_LED = 12, TTL_LED = 13, READY_LED = 15;   // on the MCP
static const uint8_t TTL_BLOCK2_OE = 10, TTL_BLOCK2_DIR = 8, TTL_BLOCK1_OE = 9, TTL_BLOCK1_DIR = 7;
// Lines 0-8 are Teensy pins; 9-15 are MCP23017 GPIOA bits (see setTtl).
static const uint8_t TTL_PIN[16] = {5, 6, 7, 8, 14, 15, 16, 17, 20, 0, 1, 2, 3, 4, 5, 6};
static const uint8_t DAC_SPAN_PLUS_MINUS_10V = 3;
static const double DAC_LOW = -10.0, DAC_HIGH = 10.0;

// A STOP or a heartbeat waits at most this long for the executor's attention:
// the slice a busy-wait returns after. 1 ms keeps STOP latency and heartbeat
// jitter invisible next to the host's reply timeout while the loop overhead
// stays negligible against the microsecond grid.
static const uint32_t SLICE_TICKS = 1000;

Adafruit_MCP23017 mcp;
static uint16_t ttlActive = 0;

// -- the LTC2668 (ported) ------------------------------------------------------------

static void spiTransferBlock(uint8_t cs, uint8_t* tx, uint8_t* rx, uint8_t length) {
    digitalWrite(cs, LOW);
    for (int8_t i = length - 1; i >= 0; --i) rx[i] = SPI.transfer(tx[i]);
    digitalWrite(cs, HIGH);
}

static void dacWrite(uint8_t command, uint8_t address, uint16_t code) {
    // command 0: write and update channel n (0x30); 1: span (0x60);
    // 2: write all and update all (0xA0).
    uint8_t tx[4], rx[4];
    tx[3] = 0;
    tx[2] = (command == 0 ? 0x30 : command == 1 ? 0x60 : 0xA0) | address;
    tx[1] = (uint8_t)(code >> 8);
    tx[0] = (uint8_t)(code & 0xFF);
    spiTransferBlock(DAC_CS, tx, rx, 4);
}

static void setDacSpan(uint8_t channel, uint8_t span) {
    dacWrite(1, channel, span);
    dacWrite(2, 0, 0);
}

static void setDacVolts(uint8_t channel, double volts) {
    double code = (volts - DAC_LOW) / (DAC_HIGH - DAC_LOW) * 65535.0;
    long c = lrint(code);
    if (c < 0) c = 0;
    if (c > 65535) c = 65535;
    dacWrite(0, channel, (uint16_t)c);
}

// -- the TTL lines (ported, including the expander's masking) ------------------------

static void setTtl(uint8_t line, uint8_t level) {
    bitWrite(ttlActive, line, level);
    if (line < 9) {
        digitalWriteFast(TTL_PIN[line], level);
        return;
    }
    // Lines 9-15 live on the expander's GPIOA, shifted one bit; the old
    // sketch's sequence for the second level shifter is kept as it was.
    uint8_t mask = highByte(ttlActive) >> 1;
    if (line > 11) {
        bitWrite(mask, 7, 1);
        mcp.writeGPIO(mask, 0);
        bitWrite(mask, 7, 0);
        mcp.writeGPIO(mask, 0);
    } else {
        bitWrite(mask, 7, 0);
        mcp.writeGPIO(mask, 0);
    }
}

// -- the trigger input -------------------------------------------------------------

static volatile uint32_t arrivalMicros[16];
static volatile uint8_t arrivalHead = 0, arrivalTail = 0;

static void onTrigger() {
    uint8_t next = (uint8_t)((arrivalHead + 1) & 15);
    if (next == arrivalTail) return;      // a full ring drops the newest
    arrivalMicros[arrivalHead] = micros();
    arrivalHead = next;
}

// -- the clock: micros(), extended to 64 bits ----------------------------------------

static uint64_t clockBase = 0;
static uint32_t clockLast = 0;

static uint64_t nowTicks() {
    uint32_t m = micros();
    if (m < clockLast) clockBase += (uint64_t)1 << 32;
    clockLast = m;
    return clockBase + m;
}

static uint64_t extend(uint32_t m) {
    // An ISR timestamp, within the last 71 minutes, on the 64-bit clock.
    uint64_t now = nowTicks();
    uint32_t low = (uint32_t)now;
    uint64_t base = now - low;
    if (m > low) base -= (uint64_t)1 << 32;
    return base + m;
}

// -- the Hal ---------------------------------------------------------------------------

class TriggerScopeHal : public stepfw::Hal {
public:
    uint64_t now() override { return nowTicks(); }

    void sleepUntil(uint64_t t) override {
        uint64_t start = nowTicks();
        while (true) {
            uint64_t n = nowTicks();
            if (n >= t || n - start >= SLICE_TICKS) return;
        }
    }

    Arrival waitArrival(uint64_t start, bool hasDeadline, uint64_t deadline,
                        uint64_t* at) override {
        uint64_t began = nowTicks();
        while (true) {
            while (arrivalTail != arrivalHead) {
                uint64_t when = extend(arrivalMicros[arrivalTail]);
                arrivalTail = (uint8_t)((arrivalTail + 1) & 15);
                if (when < start) continue;
                if (hasDeadline && when > deadline) break;
                *at = when;
                return ARRIVED;
            }
            uint64_t n = nowTicks();
            if (hasDeadline && n >= deadline) return TIMED_OUT;
            if (n - began >= SLICE_TICKS) return SLICE;
        }
    }

    int readByte() override { return Serial.available() ? Serial.read() : -1; }

    void writeLine(const char* line) override {
        Serial.write(line);
        Serial.write('\n');
    }

    void writeDac(uint8_t index, double volts) override { setDacVolts(index, volts); }
    void writeTtl(uint8_t index, uint8_t level) override { setTtl(index, level); }
};

// -- the board -------------------------------------------------------------------------

#if defined(__IMXRT1062__)
// Teensy 4.x: the log lives in OCRAM, away from the tightly coupled memory.
static const uint32_t LOG_CAPACITY = 8192;
DMAMEM static stepfw::LogEntry logEntries[LOG_CAPACITY];
#else
static const uint32_t LOG_CAPACITY = 1024;
static stepfw::LogEntry logEntries[LOG_CAPACITY];
#endif

static stepfw::BoardInfo boardInfo;
static stepfw::Log boardLog(logEntries, LOG_CAPACITY);
static TriggerScopeHal hal;
static stepfw::Board* board = NULL;

static void describeBoard() {
    memset(&boardInfo, 0, sizeof boardInfo);
    boardInfo.board = "TriggerScope-16-R4";
    boardInfo.firmware = FIRMWARE_VERSION;
    boardInfo.gridNs = 1000;
    boardInfo.dacCount = 16;
    for (uint8_t i = 0; i < 16; ++i) {
        boardInfo.dacLow[i] = DAC_LOW;
        boardInfo.dacHigh[i] = DAC_HIGH;
        boardInfo.dacBits[i] = 16;
    }
    boardInfo.ttlCount = 16;
    boardInfo.triggerIn = true;
#ifdef TRIGGER_LATENCY_TICKS
    boardInfo.hasLatency = true;
    boardInfo.latencyTicks = TRIGGER_LATENCY_TICKS;
#else
    boardInfo.hasLatency = false;
#endif
    boardInfo.hasAlive = true;
    boardInfo.aliveTicks = 500000;
    boardInfo.maxSteps = 0xFFFFFFFFu;
    boardInfo.stepReportTicks = 100000;
}

void setup() {
    mcp.begin(MCP_ADDRESS);
    for (int i = 0; i < 16; ++i) {
        mcp.pinMode(i, OUTPUT);
        mcp.digitalWrite(i, LOW);
    }
    mcp.digitalWrite(TTL_BLOCK2_OE, LOW);
    mcp.digitalWrite(TTL_BLOCK2_DIR, HIGH);
    mcp.digitalWrite(TTL_BLOCK1_OE, LOW);
    mcp.digitalWrite(TTL_BLOCK1_DIR, LOW);
    delay(10);
    for (uint8_t i = 0; i < 9; ++i) {
        pinMode(TTL_PIN[i], OUTPUT);
        digitalWrite(TTL_PIN[i], LOW);
    }
    for (uint8_t i = 9; i < 16; ++i) {
        mcp.pinMode(TTL_PIN[i], OUTPUT);
        delay(5);
        mcp.digitalWrite(TTL_PIN[i], LOW);
        delay(10);
    }
    mcp.digitalWrite(PWR_LED, HIGH);

    pinMode(TRIGGER_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(TRIGGER_PIN), onTrigger, RISING);

    Serial.begin(115200);     // USB: the rate is nominal

    pinMode(DAC_CLR, OUTPUT);
    digitalWrite(DAC_CLR, LOW);
    delay(50);
    digitalWrite(DAC_CLR, HIGH);
    delay(50);
    SPI.begin();
    pinMode(DAC_CS, OUTPUT);
    SPI.beginTransaction(SPISettings(30000000, MSBFIRST, SPI_MODE0));
    for (uint8_t d = 0; d < 16; ++d) setDacSpan(d, DAC_SPAN_PLUS_MINUS_10V);
    for (uint8_t i = 0; i < 16; ++i) {
        setTtl(i, 0);
        setDacVolts(i, 0.0);
    }

    describeBoard();
    static stepfw::Board theBoard(hal, boardInfo, boardLog);
    board = &theBoard;
    mcp.digitalWrite(READY_LED, HIGH);
    // No banner: the host's first line from this board is its answer to *IDN?.
}

void loop() {
    nowTicks();               // keep the 64-bit clock extended while idle
    while (Serial.available()) {
        mcp.digitalWrite(DAC_LED, HIGH);
        board->feed(Serial.read());    // a RUN executes inside this call
        mcp.digitalWrite(DAC_LED, LOW);
    }
}
