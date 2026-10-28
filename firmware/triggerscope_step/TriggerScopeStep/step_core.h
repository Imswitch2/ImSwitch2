// The step-engine protocol, IMSWITCH_STEP/1, as firmware: the board end of
// docs/devices/step-engine-protocol.rst, written against the reference
// implementation in imswitch/imcontrol/model/managers/scan_engines/
// (step_protocol.py decodes a program the way this does; step_executor.py
// walks it the way this does, and the log this returns must match its log
// entry for entry).
//
// Everything here is platform independent, C++11, and allocates nothing
// after construction: the same code runs on the TriggerScope (the sketch in
// this folder provides the Hal) and on a PC as a fake board with a virtual
// clock (../host), which is how it is tested without hardware.
//
// Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later; see the end of
// step_core.cpp.

#ifndef IMSWITCH_STEP_CORE_H
#define IMSWITCH_STEP_CORE_H

#include <stdint.h>
#include <stddef.h>

namespace stepfw {

// ---------------------------------------------------------------------------
// What the platform provides. Times are ticks of the board's grid on the
// platform's own clock (since boot, say); the core keeps every run-relative
// clock itself.
// ---------------------------------------------------------------------------

class Hal {
public:
    virtual ~Hal() {}

    // The board's clock in ticks, monotonic, never wrapping.
    virtual uint64_t now() = 0;

    // Let time pass until `t`, or less: the core calls it again until now()
    // reaches `t`, and polls the serial link between calls, so a real board
    // returns after a bounded slice (a STOP must be noticed) and a virtual
    // clock simply jumps to `t`.
    virtual void sleepUntil(uint64_t t) = 0;

    enum Arrival { ARRIVED, TIMED_OUT, SLICE };
    // Wait for the trigger input: the first arrival at or after `start` --
    // ARRIVED with its time in `*at` -- or, with a deadline, TIMED_OUT once
    // now() reaches it with no arrival before it, or SLICE after a bounded
    // time so the core can poll the link (a STOP) and send its heartbeat.
    virtual Arrival waitArrival(uint64_t start, bool hasDeadline, uint64_t deadline,
                                uint64_t* at) = 0;

    // The serial link: a byte, or -1 when none is waiting; a line, which the
    // platform ends with '\n'.
    virtual int readByte() = 0;
    virtual void writeLine(const char* line) = 0;

    // The outputs.
    virtual void writeDac(uint8_t index, double volts) = 0;
    virtual void writeTtl(uint8_t index, uint8_t level) = 0;
};

// ---------------------------------------------------------------------------
// What the board declares (*IDN?, *CAPS?) and the firmware's own policies.
// ---------------------------------------------------------------------------

static const uint8_t MAX_DACS = 16;
static const uint8_t MAX_TTLS = 16;

struct BoardInfo {
    const char* board;              // no commas
    const char* firmware;
    uint32_t gridNs;
    uint8_t dacCount;
    double dacLow[MAX_DACS];
    double dacHigh[MAX_DACS];
    uint8_t dacBits[MAX_DACS];      // 0: not declared, the volts are set as sent
    uint8_t ttlCount;
    bool triggerIn;
    bool hasLatency;                // measured and declared; else trigger waits are
    uint32_t latencyTicks;          // refused by the host as unknown
    bool hasAlive;
    uint32_t aliveTicks;            // a message at least this often while running
    uint32_t maxSteps;
    // STEP,i,n is sent for the first step and then at most this often (a
    // board "may leave some out"): a progress bar needs no more, and a
    // message per microsecond step would starve the run.
    uint32_t stepReportTicks;
};

// ---------------------------------------------------------------------------
// Capacities: fixed, so the firmware allocates nothing while it runs. A
// program past any of them is refused with ERR,capacity and the limit named.
// ---------------------------------------------------------------------------

static const uint8_t MAX_LOOPS = 8;
static const uint8_t MAX_TIME_CHANNELS = 8;
static const uint8_t MAX_DRIVES = MAX_DACS + MAX_TIME_CHANNELS;
static const uint16_t TABLE_POOL = 4096;    // floats, all tables together
static const uint8_t MAX_EVENTS = 32;
static const uint8_t MAX_PHASES = 16;       // per event
static const uint8_t MAX_WAITS = 16;
static const uint8_t MAX_PREPARE = MAX_DRIVES;
static const uint8_t MAX_SAFE = MAX_DACS + MAX_TTLS;
static const uint16_t MAX_SCHEDULE = 512;   // outputs timed ahead of the clock
static const uint16_t LINE_MAX = 16384;     // a TABLE line can be long

// ---------------------------------------------------------------------------
// The program as the board holds it.
// ---------------------------------------------------------------------------

enum ChannelKind : uint8_t { CH_DAC, CH_TTL, CH_TIME };
struct Channel { uint8_t kind; uint8_t index; };

enum Traversal : uint8_t { FORWARD, REVERSE, SERPENTINE };
enum Placement : uint8_t { IN_STEP, BETWEEN_STEPS };
enum Origin : uint8_t { ABSOLUTE, CAPTURED };
enum Restore : uint8_t { RESTORE_CAPTURED, RESTORE_FIRST, RESTORE_PARK, RESTORE_HOLD };
enum Anchor : uint8_t { AT_STEP, PASS_START, PASS_END };
enum WaitAt : uint8_t { WAIT_START = 0xFE, WAIT_STEP = 0xFF };   // else a loop index

static const int8_t NO_LOOP = -1;

struct Loop {
    uint32_t count;
    uint8_t traversal;
    uint8_t parityCount;
    uint8_t parity[MAX_LOOPS];
};

struct Drive {
    Channel channel;
    double offset;
    bool hasWeight[MAX_LOOPS];
    double weight[MAX_LOOPS];
    int8_t tableLoop;
    uint16_t tableStart;        // into the pool
    uint32_t tableCount;
    uint32_t move, settle;
    uint8_t placement, origin, restore;
};

struct Event {
    Channel channel;
    int32_t start, end;
    uint8_t anchor;
    int8_t anchorLoop;
    bool allPhases;
    uint8_t phaseCount;
    uint32_t phases[MAX_PHASES];
    double on, off;
};

struct Wait {
    uint8_t at;
    bool trigger;               // else time
    bool hasInterval; uint32_t interval;
    bool hasTimeout; uint32_t timeout;
    bool failOnTimeout;         // else continue
};

struct ChannelValue { Channel channel; double value; };
struct ChannelTicks { Channel channel; uint32_t ticks; };

struct Program {
    uint8_t loopCount;
    Loop loops[MAX_LOOPS];
    int8_t phaseLoop;
    bool hasStep;
    uint32_t stepTicks, returnTicks;
    uint8_t driveCount;
    Drive drives[MAX_DRIVES];
    float table[TABLE_POOL];
    uint16_t tableUsed;
    uint8_t eventCount;
    Event events[MAX_EVENTS];
    uint8_t waitCount;
    Wait waits[MAX_WAITS];
    uint8_t prepareCount;
    ChannelTicks prepare[MAX_PREPARE];
    uint8_t capturedCount;
    ChannelValue captured[MAX_DRIVES];
    uint8_t parkCount;
    ChannelValue park[MAX_DRIVES];
    uint8_t safeCount;
    ChannelValue safe[MAX_SAFE];
    uint32_t steps;             // the product of the counts
};

// ---------------------------------------------------------------------------
// The log of a run: what LOG? answers. Ticks are signed: an edge may be
// timed before its step (the executor logs it so too).
// ---------------------------------------------------------------------------

enum LogKind : uint8_t { LOG_DAC, LOG_ON, LOG_OFF, LOG_RELEASE, LOG_FINISH, LOG_SAFE, LOG_FAILED };

static const int64_t NO_ARRIVAL = -1;

struct LogEntry {
    uint8_t kind;
    Channel channel;            // DAC, ON, OFF
    uint32_t segment;
    int64_t tick;               // the segment's tick; the clock for SAFE and FAILED
    union {
        double value;           // DAC volts, ON/OFF level: a double, so what the
                                // host reads back is the code's exact volts
        int64_t clock;          // RELEASE: the arrival (or NO_ARRIVAL); FINISH: the clock
    } u;
};

class Log {
public:
    Log(LogEntry* entries, uint32_t capacity);
    void clear();
    bool add(const LogEntry& entry);    // false once full: the run still runs
    uint32_t count() const { return count_; }
    bool overflowed() const { return overflowed_; }
    const LogEntry& at(uint32_t i) const { return entries_[i]; }
    uint32_t capacity() const { return capacity_; }
    void setFailure(const char* reason);
    const char* failure() const { return failure_; }
private:
    LogEntry* entries_;
    uint32_t capacity_;
    uint32_t count_;
    bool overflowed_;
    char failure_[160];
};

// ---------------------------------------------------------------------------
// The board.
// ---------------------------------------------------------------------------

class Board {
public:
    Board(Hal& hal, const BoardInfo& info, Log& log);

    // Feed the link's bytes; a complete line is acted on at once. A RUN
    // executes the program inside this call (the core polls the link itself
    // while it runs), so the platform's loop is: feed what arrives.
    void feed(int byte);

    bool running() const { return running_; }

    // Test hook (the fake board on a PC): fail at a step, as the simulated
    // board's fail_at_step. Not reachable over the protocol.
    void failAt(bool enabled, uint32_t step) { failEnabled_ = enabled; failStep_ = step; }

private:
    // -- the link
    void line(char* text);
    void command(char* text);
    void reply(const char* text);
    void replyf(const char* fmt, ...);
    void error(const char* code, const char* reason);
    void sendIdn();
    void sendCaps();
    void sendLog();
    void set(char* fields);

    // -- a program
    void beginProgram();
    void programLine(char* text);
    void endProgram();
    bool parseLine(char* text);
    bool check();
    void fail(const char* code, const char* fmt, ...);
    bool parseChannel(const char* token, Channel* out);
    bool hasChannel(const Channel& c) const;
    bool parseUint(const char* token, uint32_t* out);
    bool parseInt(const char* token, int32_t* out);
    bool parseFloat(const char* token, double* out);
    bool parseLoopRef(const char* token, int8_t* out);
    bool parseLevel(const Channel& c, const char* token, double* out);
    Drive* findDrive(const Channel& c);

    // -- a run
    void run();
    bool walk();                            // false: stopped or failed
    bool runFailed(const char* fmt, ...);
    bool stopped();
    double position(const Drive& d, const uint32_t* counters) const;
    double settable(const Channel& c, double value) const;
    uint8_t heldIndex(const Channel& c) const;
    void write(const Channel& c, double value, bool hasTick, int64_t tick);
    void edge(const Event& e, int64_t start, int64_t end);
    void advanceTo(int64_t tick);
    bool schedule(int64_t wall, const Channel& c, double value);
    void drain(int64_t untilWall);          // outputs due by then, in time order
    bool wait(const Wait& w, const double* here);
    void safeState();
    void heartbeat(const char* kind);
    void poll();                            // the link while running
    int64_t nowRel() const;                 // the run clock: ticks since RUN
    void passTime(int64_t untilRel, const char* heartbeatKind);
    int64_t wallOf(int64_t tick) const { return wall_ + (tick - tick_); }
    double restoreTarget(const Drive& d, const double* first, const double* here) const;

    // -- the loop counters
    struct Counters {
        uint32_t counter[MAX_LOOPS];
        uint32_t ordinal[MAX_LOOPS];
        bool reversed[MAX_LOOPS];
    };
    void startPass(Counters& c, uint8_t depth) const;
    void firstVisit(Counters& c) const;
    bool nextVisit(Counters& c) const;      // false at the end
    bool outerDiffers(const Counters& a, const Counters& b, uint8_t depth) const;

    Hal& hal_;
    const BoardInfo& info_;
    Log& log_;

    // the link
    char line_[LINE_MAX + 1];
    uint32_t lineLength_;
    bool lineOverflow_;

    // programs: the one being received, and the one that runs
    Program incoming_;
    Program program_;
    bool hasProgram_;
    bool receiving_;
    bool failed_;                   // of an upload, or of a SET's parse
    char errorCode_[12];
    char errorText_[200];
    int lineNumber_;

    // a run
    bool running_;
    bool stop_;
    uint32_t segment_;
    int64_t wall_, tick_;           // (executor) _wall, _tick
    uint64_t runStart_;             // hal.now() at RUN: wall_ is relative to it
    uint32_t step_;
    uint64_t lastMessageAt_, lastStepReportAt_;
    bool failEnabled_; uint32_t failStep_;
    char failure_[160];
    bool hasHeld_[MAX_DRIVES + 1];  // drives by index; one spare slot
    double held_[MAX_DRIVES + 1];
    struct Scheduled { int64_t wall; Channel channel; double value; };
    Scheduled schedule_[MAX_SCHEDULE];
    uint16_t scheduled_;
};

}  // namespace stepfw

#endif
