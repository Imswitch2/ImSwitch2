// IMSWITCH_STEP/1 as firmware. See step_core.h.
//
// The executor mirrors step_executor.py's StepExecutor._walk statement by
// statement; where this file says "(executor)" the reference is that walk.
// The one thing it adds is time: the executor only logs what it would do at
// which tick, this also does it, when the clock says so.

#include "step_core.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <errno.h>

namespace stepfw {

static const char* const PROTOCOL = "IMSWITCH_STEP/1";
static const char* const E_SYNTAX = "syntax";
static const char* const E_RANGE = "range";
static const char* const E_STATE = "state";
static const char* const E_CAPACITY = "capacity";
static const char* const E_CHANNEL = "channel";

static const uint32_t UINT32_MAX_ = 0xFFFFFFFFu;

// ---------------------------------------------------------------------------
// Log
// ---------------------------------------------------------------------------

Log::Log(LogEntry* entries, uint32_t capacity)
    : entries_(entries), capacity_(capacity), count_(0), overflowed_(false) {
    failure_[0] = 0;
}

void Log::clear() {
    count_ = 0;
    overflowed_ = false;
    failure_[0] = 0;
}

bool Log::add(const LogEntry& entry) {
    if (count_ >= capacity_) {
        overflowed_ = true;
        return false;
    }
    entries_[count_++] = entry;
    return true;
}

void Log::setFailure(const char* reason) {
    strncpy(failure_, reason, sizeof failure_ - 1);
    failure_[sizeof failure_ - 1] = 0;
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

static void channelName(const Channel& c, char* out, size_t size) {
    snprintf(out, size, "%s%u", c.kind == CH_DAC ? "dac" : c.kind == CH_TTL ? "ttl" : "time",
             (unsigned)c.index);
}

static bool sameChannel(const Channel& a, const Channel& b) {
    return a.kind == b.kind && a.index == b.index;
}

// A number as the wire writes it: enough digits for a double to round-trip,
// so a DAC code's volts read back as the exact value the host computes.
static void fmtValue(double v, char* out, size_t size) {
    snprintf(out, size, "%.17g", v);
}

static char* nextField(char** cursor) {
    // The next comma-separated field, or NULL when the line is exhausted.
    char* start = *cursor;
    if (start == NULL) return NULL;
    char* comma = strchr(start, ',');
    if (comma != NULL) {
        *comma = 0;
        *cursor = comma + 1;
    } else {
        *cursor = NULL;
    }
    return start;
}

static int countFields(const char* text) {
    int n = 1;
    for (; *text; ++text) if (*text == ',') ++n;
    return n;
}

static char* trimmed(char* s) {
    while (*s == ' ' || *s == '\t') ++s;
    char* end = s + strlen(s);
    while (end > s && (end[-1] == ' ' || end[-1] == '\t')) *--end = 0;
    return s;
}

static Channel timeChannel() {
    Channel c;
    c.kind = CH_TIME;
    c.index = 0;
    return c;
}

// ---------------------------------------------------------------------------
// Board: the link
// ---------------------------------------------------------------------------

Board::Board(Hal& hal, const BoardInfo& info, Log& log)
    : hal_(hal), info_(info), log_(log), lineLength_(0), lineOverflow_(false),
      hasProgram_(false), receiving_(false), failed_(false), lineNumber_(0),
      running_(false), stop_(false), segment_(0), wall_(0), tick_(0), runStart_(0),
      step_(0), lastMessageAt_(0), lastStepReportAt_(0), failEnabled_(false),
      failStep_(0), scheduled_(0) {
    errorCode_[0] = errorText_[0] = failure_[0] = 0;
    memset(&incoming_, 0, sizeof incoming_);
    memset(&program_, 0, sizeof program_);
    memset(hasHeld_, 0, sizeof hasHeld_);
}

void Board::feed(int byte) {
    if (byte < 0) return;
    if (byte == '\n') {
        if (lineOverflow_) {
            lineOverflow_ = false;
            lineLength_ = 0;
            if (receiving_) {
                fail(E_CAPACITY, "a line longer than %u characters", (unsigned)LINE_MAX);
            } else {
                error(E_SYNTAX, "a line longer than the board reads");
            }
            return;
        }
        line_[lineLength_] = 0;
        lineLength_ = 0;
        size_t n = strlen(line_);
        if (n && line_[n - 1] == '\r') line_[n - 1] = 0;
        // A RUN executes inside line(), and the run polls the link into
        // line_ again: the text is not used after the command dispatches.
        line(line_);
        return;
    }
    if (lineLength_ >= LINE_MAX) {
        lineOverflow_ = true;
        return;
    }
    line_[lineLength_++] = (char)byte;
}

void Board::line(char* text) {
    char* t = trimmed(text);
    if (*t == 0) return;
    if (receiving_) programLine(t);
    else command(t);
}

void Board::reply(const char* text) {
    hal_.writeLine(text);
    lastMessageAt_ = hal_.now();
}

void Board::replyf(const char* fmt, ...) {
    char buffer[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buffer, sizeof buffer, fmt, args);
    va_end(args);
    reply(buffer);
}

void Board::error(const char* code, const char* reason) {
    replyf("ERR,%s,%s", code, reason);
}

void Board::command(char* text) {
    if (strcmp(text, "*IDN?") == 0) {
        sendIdn();
    } else if (strcmp(text, "*CAPS?") == 0) {
        sendCaps();
    } else if (strcmp(text, "PROGRAM,BEGIN") == 0) {
        if (running_) error(E_STATE, "a program cannot be uploaded while one runs");
        else beginProgram();
    } else if (strcmp(text, "RUN") == 0) {
        if (running_) error(E_STATE, "already running");
        else if (!hasProgram_) error(E_STATE, "no program to run");
        else run();
    } else if (strcmp(text, "STOP") == 0) {
        stop_ = true;
        reply("OK,STOP");
    } else if (strcmp(text, "LOG?") == 0) {
        if (running_) error(E_STATE, "the log comes after the run");
        else sendLog();
    } else if (strncmp(text, "SET,", 4) == 0) {
        if (running_) error(E_STATE, "an output is not set by hand while a program runs");
        else set(text + 4);
    } else {
        char buffer[200];
        snprintf(buffer, sizeof buffer, "not a command of the protocol: %.150s", text);
        error(E_SYNTAX, buffer);
    }
}

void Board::sendIdn() {
    replyf("%s,%s,%s", PROTOCOL, info_.board, info_.firmware);
}

void Board::sendCaps() {
    char lo[32], hi[32];
    replyf("CAPS,grid_ns,%lu", (unsigned long)info_.gridNs);
    for (uint8_t i = 0; i < info_.dacCount; ++i) {
        fmtValue(info_.dacLow[i], lo, sizeof lo);
        fmtValue(info_.dacHigh[i], hi, sizeof hi);
        replyf("CAPS,dac,%u,%s,%s", (unsigned)i, lo, hi);
    }
    replyf("CAPS,ttl,%u", (unsigned)info_.ttlCount);
    replyf("CAPS,max_steps,%lu", (unsigned long)info_.maxSteps);
    replyf("CAPS,trigger_in,%u", info_.triggerIn ? 1u : 0u);
    if (info_.hasLatency) replyf("CAPS,trigger_latency_ticks,%lu", (unsigned long)info_.latencyTicks);
    for (uint8_t i = 0; i < info_.dacCount; ++i) {
        if (info_.dacBits[i]) replyf("CAPS,dac_bits,%u,%u", (unsigned)i, (unsigned)info_.dacBits[i]);
    }
    if (info_.hasAlive) replyf("CAPS,alive_ticks,%lu", (unsigned long)info_.aliveTicks);
    reply("CAPS,END");
}

void Board::sendLog() {
    if (log_.overflowed()) {
        replyf("ERR,%s,the log holds %lu entries and the run wrote more", E_CAPACITY,
               (unsigned long)log_.capacity());
        return;
    }
    char name[16], value[32];
    for (uint32_t i = 0; i < log_.count(); ++i) {
        const LogEntry& e = log_.at(i);
        switch (e.kind) {
        case LOG_DAC:
            channelName(e.channel, name, sizeof name);
            fmtValue(e.u.value, value, sizeof value);
            replyf("LOG,DAC,%lu,%lld,%s,%s", (unsigned long)e.segment, (long long)e.tick, name,
                   value);
            break;
        case LOG_ON:
        case LOG_OFF:
            channelName(e.channel, name, sizeof name);
            fmtValue(e.u.value, value, sizeof value);
            replyf("LOG,%s,%lu,%lld,%s,%s", e.kind == LOG_ON ? "ON" : "OFF",
                   (unsigned long)e.segment, (long long)e.tick, name, value);
            break;
        case LOG_RELEASE:
            if (e.u.clock == NO_ARRIVAL)
                replyf("LOG,RELEASE,%lu,%lld,-", (unsigned long)e.segment, (long long)e.tick);
            else
                replyf("LOG,RELEASE,%lu,%lld,%lld", (unsigned long)e.segment, (long long)e.tick,
                       (long long)e.u.clock);
            break;
        case LOG_FINISH:
            replyf("LOG,FINISH,%lu,%lld,%lld", (unsigned long)e.segment, (long long)e.tick,
                   (long long)e.u.clock);
            break;
        case LOG_SAFE:
            replyf("LOG,SAFE,%lld", (long long)e.tick);
            break;
        case LOG_FAILED:
            replyf("LOG,FAILED,%lld,%s", (long long)e.tick, log_.failure());
            break;
        }
    }
    reply("LOG,END");
}

void Board::set(char* fields) {
    // SET,<channel>,<value>: an output by hand, between runs.
    if (countFields(fields) != 2) {
        error(E_SYNTAX, "SET takes a channel and a value");
        return;
    }
    char* cursor = fields;
    const char* name = nextField(&cursor);
    const char* valueText = nextField(&cursor);
    failed_ = false;
    lineNumber_ = 0;
    Channel c;
    if (!parseChannel(name, &c) || c.kind == CH_TIME || !hasChannel(c)) {
        char buffer[120];
        snprintf(buffer, sizeof buffer, "%.60s: this board has no such output", name);
        error(E_CHANNEL, buffer);
        return;
    }
    double value;
    if (!parseLevel(c, valueText, &value)) {
        error(errorCode_, errorText_);
        return;
    }
    if (c.kind == CH_DAC) {
        double low = info_.dacLow[c.index], high = info_.dacHigh[c.index];
        if (!(low <= value && value <= high)) {
            char buffer[120];
            snprintf(buffer, sizeof buffer, "%s gives %g to %g V, not %g", name, low, high, value);
            error(E_RANGE, buffer);
            return;
        }
        hal_.writeDac(c.index, settable(c, value));
    } else {
        hal_.writeTtl(c.index, (uint8_t)value);
    }
    reply("OK,SET");
}

// ---------------------------------------------------------------------------
// Board: receiving a program (step_protocol.lines_to_plan, and the simulated
// board's _check)
// ---------------------------------------------------------------------------

void Board::fail(const char* code, const char* fmt, ...) {
    // The first problem of an upload is the one reported; the rest of the
    // upload is still read to its end so the link stays in step.
    if (failed_) return;
    failed_ = true;
    strncpy(errorCode_, code, sizeof errorCode_ - 1);
    errorCode_[sizeof errorCode_ - 1] = 0;
    char buffer[160];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buffer, sizeof buffer, fmt, args);
    va_end(args);
    if (lineNumber_ > 0)
        snprintf(errorText_, sizeof errorText_, "line %d: %s", lineNumber_, buffer);
    else
        snprintf(errorText_, sizeof errorText_, "%s", buffer);
}

void Board::beginProgram() {
    memset(&incoming_, 0, sizeof incoming_);
    incoming_.phaseLoop = NO_LOOP;
    receiving_ = true;
    failed_ = false;
    lineNumber_ = 1;
    errorCode_[0] = errorText_[0] = 0;
}

void Board::programLine(char* text) {
    ++lineNumber_;
    if (strcmp(text, "PROGRAM,END") == 0) {
        endProgram();
        return;
    }
    if (failed_) return;
    parseLine(text);
}

void Board::endProgram() {
    receiving_ = false;
    lineNumber_ = 0;            // what check() finds is the program's, not a line's
    if (!failed_) check();
    if (failed_) {
        error(errorCode_, errorText_);
        return;
    }
    program_ = incoming_;
    hasProgram_ = true;
    replyf("OK,PROGRAM,%lu", (unsigned long)program_.steps);
}

bool Board::parseChannel(const char* token, Channel* out) {
    // dac<i>, ttl<j>, time<k>; ERR,channel otherwise.
    const char* digits = NULL;
    if (strncmp(token, "dac", 3) == 0) { out->kind = CH_DAC; digits = token + 3; }
    else if (strncmp(token, "ttl", 3) == 0) { out->kind = CH_TTL; digits = token + 3; }
    else if (strncmp(token, "time", 4) == 0) { out->kind = CH_TIME; digits = token + 4; }
    if (digits == NULL || *digits == 0) {
        fail(E_CHANNEL, "%.40s is not a channel (dac<i>, ttl<j> or time<k>)", token);
        return false;
    }
    for (const char* p = digits; *p; ++p) {
        if (*p < '0' || *p > '9') {
            fail(E_CHANNEL, "%.40s is not a channel (dac<i>, ttl<j> or time<k>)", token);
            return false;
        }
    }
    long index = strtol(digits, NULL, 10);
    if (index < 0 || index > 255) {
        fail(E_CHANNEL, "%.40s: no board has that many", token);
        return false;
    }
    out->index = (uint8_t)index;
    return true;
}

bool Board::hasChannel(const Channel& c) const {
    if (c.kind == CH_TIME) return c.index < MAX_TIME_CHANNELS;
    if (c.kind == CH_DAC) return c.index < info_.dacCount;
    return c.index < info_.ttlCount;
}

bool Board::parseUint(const char* token, uint32_t* out) {
    // A whole number that fits 32 unsigned bits (the protocol's counts,
    // durations and indices). Python's int() takes surrounding spaces and a
    // sign; a value that does not fit is ERR,range, never a wrap.
    char* end;
    errno = 0;
    const char* t = token;
    while (*t == ' ') ++t;
    if (*t == 0) { fail(E_SYNTAX, "'%.40s' is not a whole number", token); return false; }
    long long v = strtoll(t, &end, 10);
    while (*end == ' ') ++end;
    if (*end != 0 || errno == ERANGE) {
        if (errno == ERANGE) fail(E_RANGE, "%.40s does not fit an unsigned 32-bit number", token);
        else fail(E_SYNTAX, "'%.40s' is not a whole number", token);
        return false;
    }
    if (v < 0 || v > (long long)UINT32_MAX_) {
        fail(E_RANGE, "%lld does not fit an unsigned 32-bit number", v);
        return false;
    }
    *out = (uint32_t)v;
    return true;
}

bool Board::parseInt(const char* token, int32_t* out) {
    char* end;
    errno = 0;
    const char* t = token;
    while (*t == ' ') ++t;
    if (*t == 0) { fail(E_SYNTAX, "'%.40s' is not a whole number", token); return false; }
    long long v = strtoll(t, &end, 10);
    while (*end == ' ') ++end;
    if (*end != 0 || errno == ERANGE) {
        if (errno == ERANGE) fail(E_RANGE, "%.40s does not fit a signed 32-bit number", token);
        else fail(E_SYNTAX, "'%.40s' is not a whole number", token);
        return false;
    }
    if (v < -2147483648LL || v > 2147483647LL) {
        fail(E_RANGE, "%lld does not fit a signed 32-bit number", v);
        return false;
    }
    *out = (int32_t)v;
    return true;
}

bool Board::parseFloat(const char* token, double* out) {
    char* end;
    const char* t = token;
    while (*t == ' ') ++t;
    if (*t == 0) { fail(E_SYNTAX, "'%.40s' is not a number", token); return false; }
    double v = strtod(t, &end);
    while (*end == ' ') ++end;
    if (*end != 0 || isnan(v) || isinf(v)) {
        fail(E_SYNTAX, "'%.40s' is not a number", token);
        return false;
    }
    *out = v;
    return true;
}

bool Board::parseLoopRef(const char* token, int8_t* out) {
    if (strcmp(token, "-") == 0) { *out = NO_LOOP; return true; }
    uint32_t i;
    if (!parseUint(token, &i)) return false;
    if (i >= incoming_.loopCount) {
        fail(E_RANGE, "loop %lu is not declared before it", (unsigned long)i);
        return false;
    }
    *out = (int8_t)i;
    return true;
}

bool Board::parseLevel(const Channel& c, const char* token, double* out) {
    // A line's level is 0 or 1; a DAC's is volts.
    if (c.kind == CH_TTL) {
        uint32_t level;
        if (!parseUint(token, &level)) return false;
        if (level > 1) {
            fail(E_RANGE, "%lu does not fit a line level (0 or 1)", (unsigned long)level);
            return false;
        }
        *out = level;
        return true;
    }
    return parseFloat(token, out);
}

Drive* Board::findDrive(const Channel& c) {
    for (uint8_t i = 0; i < incoming_.driveCount; ++i)
        if (sameChannel(incoming_.drives[i].channel, c)) return &incoming_.drives[i];
    return NULL;
}

bool Board::parseLine(char* text) {
    Program& p = incoming_;
    int fields = countFields(text) - 1;
    char* cursor = text;
    const char* kind = nextField(&cursor);
    char* f[8];
    int n = 0;
    while (n < 8) {
        char* field = nextField(&cursor);
        if (field == NULL) break;
        f[n++] = field;
    }
    Channel c;

    if (strcmp(kind, "LOOP") == 0 && fields == 3) {
        if (p.loopCount >= MAX_LOOPS) { fail(E_CAPACITY, "this board holds %u loops", (unsigned)MAX_LOOPS); return false; }
        Loop& loop = p.loops[p.loopCount];
        memset(&loop, 0, sizeof loop);
        if (!parseUint(f[0], &loop.count)) return false;
        if (loop.count == 0) { fail(E_RANGE, "a loop of 0 counts runs nothing"); return false; }
        if (strcmp(f[1], "forward") == 0) loop.traversal = FORWARD;
        else if (strcmp(f[1], "reverse") == 0) loop.traversal = REVERSE;
        else if (strcmp(f[1], "serpentine") == 0) loop.traversal = SERPENTINE;
        else { fail(E_SYNTAX, "traversal '%.20s' is not one of forward, reverse, serpentine", f[1]); return false; }
        if (strcmp(f[2], "-") != 0) {
            char* items = f[2];
            while (items != NULL) {
                char* semi = strchr(items, ';');
                if (semi) *semi = 0;
                int8_t ref;
                if (!parseLoopRef(items, &ref)) return false;
                if (loop.parityCount >= MAX_LOOPS) { fail(E_CAPACITY, "too many parity loops"); return false; }
                loop.parity[loop.parityCount++] = (uint8_t)ref;
                items = semi ? semi + 1 : NULL;
            }
        }
        ++p.loopCount;
        return true;
    }
    if (strcmp(kind, "PHASE") == 0 && fields == 1) {
        return parseLoopRef(f[0], &p.phaseLoop);
    }
    if (strcmp(kind, "STEP") == 0 && fields == 2) {
        if (!parseUint(f[0], &p.stepTicks) || !parseUint(f[1], &p.returnTicks)) return false;
        p.hasStep = true;
        return true;
    }
    if (strcmp(kind, "DRIVE") == 0 && fields == 7) {
        if (!parseChannel(f[0], &c)) return false;
        Drive* existing = findDrive(c);
        if (existing == NULL) {
            if (p.driveCount >= MAX_DRIVES) { fail(E_CAPACITY, "this board holds %u drives", (unsigned)MAX_DRIVES); return false; }
            existing = &p.drives[p.driveCount++];
        }
        Drive& d = *existing;
        memset(&d, 0, sizeof d);
        d.channel = c;
        d.tableLoop = NO_LOOP;
        if (!parseFloat(f[1], &d.offset)) return false;
        if (!parseUint(f[2], &d.move) || !parseUint(f[3], &d.settle)) return false;
        if (strcmp(f[4], "in-step") == 0) d.placement = IN_STEP;
        else if (strcmp(f[4], "between-steps") == 0) d.placement = BETWEEN_STEPS;
        else { fail(E_SYNTAX, "placement '%.20s' is not one of in-step, between-steps", f[4]); return false; }
        if (strcmp(f[5], "absolute") == 0) d.origin = ABSOLUTE;
        else if (strcmp(f[5], "captured") == 0) d.origin = CAPTURED;
        else { fail(E_SYNTAX, "origin '%.20s' is not one of absolute, captured", f[5]); return false; }
        if (strcmp(f[6], "captured") == 0) d.restore = RESTORE_CAPTURED;
        else if (strcmp(f[6], "first") == 0) d.restore = RESTORE_FIRST;
        else if (strcmp(f[6], "park") == 0) d.restore = RESTORE_PARK;
        else if (strcmp(f[6], "hold") == 0) d.restore = RESTORE_HOLD;
        else { fail(E_SYNTAX, "restore '%.20s' is not one of captured, first, park, hold", f[6]); return false; }
        return true;
    }
    if (strcmp(kind, "WEIGHT") == 0 && fields == 3) {
        if (!parseChannel(f[0], &c)) return false;
        Drive* d = findDrive(c);
        if (d == NULL) { fail(E_SYNTAX, "%.20s has no DRIVE line before it", f[0]); return false; }
        int8_t loop;
        if (!parseLoopRef(f[1], &loop)) return false;
        if (loop == NO_LOOP) { fail(E_SYNTAX, "a weight needs a loop"); return false; }
        double w;
        if (!parseFloat(f[2], &w)) return false;
        d->hasWeight[loop] = true;
        d->weight[loop] = w;
        return true;
    }
    if (strcmp(kind, "TABLE") == 0 && fields == 3) {
        if (!parseChannel(f[0], &c)) return false;
        Drive* d = findDrive(c);
        if (d == NULL) { fail(E_SYNTAX, "%.20s has no DRIVE line before it", f[0]); return false; }
        int8_t loop;
        if (!parseLoopRef(f[1], &loop)) return false;
        if (loop == NO_LOOP) { fail(E_SYNTAX, "a table needs a loop"); return false; }
        d->tableLoop = loop;
        d->tableStart = p.tableUsed;
        d->tableCount = 0;
        if (strcmp(f[2], "-") != 0) {
            char* items = f[2];
            while (items != NULL) {
                char* semi = strchr(items, ';');
                if (semi) *semi = 0;
                double v;
                if (!parseFloat(items, &v)) return false;
                if (p.tableUsed >= TABLE_POOL) { fail(E_CAPACITY, "this board holds %u table entries in all", (unsigned)TABLE_POOL); return false; }
                p.table[p.tableUsed++] = (float)v;
                ++d->tableCount;
                items = semi ? semi + 1 : NULL;
            }
        }
        return true;
    }
    if (strcmp(kind, "EVENT") == 0 && fields == 8) {
        if (p.eventCount >= MAX_EVENTS) { fail(E_CAPACITY, "this board holds %u events", (unsigned)MAX_EVENTS); return false; }
        Event& e = p.events[p.eventCount];
        memset(&e, 0, sizeof e);
        if (!parseChannel(f[0], &e.channel)) return false;
        if (!parseInt(f[1], &e.start) || !parseInt(f[2], &e.end)) return false;
        if (strcmp(f[3], "step") == 0) e.anchor = AT_STEP;
        else if (strcmp(f[3], "pass-start") == 0) e.anchor = PASS_START;
        else if (strcmp(f[3], "pass-end") == 0) e.anchor = PASS_END;
        else { fail(E_SYNTAX, "anchor '%.20s' is not one of step, pass-start, pass-end", f[3]); return false; }
        if (!parseLoopRef(f[4], &e.anchorLoop)) return false;
        if (e.anchor != AT_STEP && e.anchorLoop == NO_LOOP) { fail(E_SYNTAX, "a pass anchor needs its loop"); return false; }
        if (strcmp(f[5], "-") == 0) {
            e.allPhases = true;
        } else {
            char* items = f[5];
            while (items != NULL) {
                char* semi = strchr(items, ';');
                if (semi) *semi = 0;
                uint32_t phase;
                if (!parseUint(items, &phase)) return false;
                if (e.phaseCount >= MAX_PHASES) { fail(E_CAPACITY, "this board holds %u phases per event", (unsigned)MAX_PHASES); return false; }
                e.phases[e.phaseCount++] = phase;
                items = semi ? semi + 1 : NULL;
            }
        }
        if (!parseLevel(e.channel, f[6], &e.on) || !parseLevel(e.channel, f[7], &e.off)) return false;
        ++p.eventCount;
        return true;
    }
    if (strcmp(kind, "WAIT") == 0 && fields == 5) {
        if (p.waitCount >= MAX_WAITS) { fail(E_CAPACITY, "this board holds %u waits", (unsigned)MAX_WAITS); return false; }
        Wait& w = p.waits[p.waitCount];
        memset(&w, 0, sizeof w);
        if (strcmp(f[0], "start") == 0) w.at = WAIT_START;
        else if (strcmp(f[0], "step") == 0) w.at = WAIT_STEP;
        else {
            int8_t loop;
            if (!parseLoopRef(f[0], &loop)) return false;
            if (loop == NO_LOOP) { fail(E_SYNTAX, "a wait is at start, step or a loop"); return false; }
            w.at = (uint8_t)loop;
        }
        if (strcmp(f[1], "time") == 0) w.trigger = false;
        else if (strcmp(f[1], "trigger") == 0) w.trigger = true;
        else { fail(E_SYNTAX, "a wait is for time or the trigger, not '%.20s'", f[1]); return false; }
        if (strcmp(f[2], "-") != 0) { if (!parseUint(f[2], &w.interval)) return false; w.hasInterval = true; }
        if (strcmp(f[3], "-") != 0) { if (!parseUint(f[3], &w.timeout)) return false; w.hasTimeout = true; }
        if (strcmp(f[4], "fail") == 0) w.failOnTimeout = true;
        else if (strcmp(f[4], "continue") == 0) w.failOnTimeout = false;
        else { fail(E_SYNTAX, "on_timeout '%.20s' is not one of fail, continue", f[4]); return false; }
        ++p.waitCount;
        return true;
    }
    if (strcmp(kind, "PREPARE") == 0 && fields == 2) {
        if (!parseChannel(f[0], &c)) return false;
        uint32_t ticks;
        if (!parseUint(f[1], &ticks)) return false;
        for (uint8_t i = 0; i < p.prepareCount; ++i) {
            if (sameChannel(p.prepare[i].channel, c)) { p.prepare[i].ticks = ticks; return true; }
        }
        if (p.prepareCount >= MAX_PREPARE) { fail(E_CAPACITY, "this board holds %u preparations", (unsigned)MAX_PREPARE); return false; }
        p.prepare[p.prepareCount].channel = c;
        p.prepare[p.prepareCount].ticks = ticks;
        ++p.prepareCount;
        return true;
    }
    if ((strcmp(kind, "CAPTURED") == 0 || strcmp(kind, "PARK") == 0) && fields == 2) {
        if (!parseChannel(f[0], &c)) return false;
        double v;
        if (!parseFloat(f[1], &v)) return false;
        ChannelValue* list = kind[0] == 'C' ? p.captured : p.park;
        uint8_t* count = kind[0] == 'C' ? &p.capturedCount : &p.parkCount;
        for (uint8_t i = 0; i < *count; ++i) {
            if (sameChannel(list[i].channel, c)) { list[i].value = v; return true; }
        }
        if (*count >= MAX_DRIVES) { fail(E_CAPACITY, "this board holds %u %s positions", (unsigned)MAX_DRIVES, kind); return false; }
        list[*count].channel = c;
        list[*count].value = v;
        ++*count;
        return true;
    }
    if (strcmp(kind, "SAFE") == 0 && fields == 2) {
        if (!parseChannel(f[0], &c)) return false;
        double v;
        if (!parseLevel(c, f[1], &v)) return false;
        for (uint8_t i = 0; i < p.safeCount; ++i) {
            if (sameChannel(p.safe[i].channel, c)) { p.safe[i].value = v; return true; }
        }
        if (p.safeCount >= MAX_SAFE) { fail(E_CAPACITY, "this board holds %u safe levels", (unsigned)MAX_SAFE); return false; }
        p.safe[p.safeCount].channel = c;
        p.safe[p.safeCount].value = v;
        ++p.safeCount;
        return true;
    }
    fail(E_SYNTAX, "not a program line of %s: %.40s", PROTOCOL, kind);
    return false;
}

bool Board::check() {
    Program& p = incoming_;
    char name[16];
    if (!p.hasStep) { fail(E_SYNTAX, "The program has no STEP line."); return false; }
    if (p.loopCount == 0) { fail(E_SYNTAX, "The program has no LOOP line."); return false; }
    for (uint8_t i = 0; i < p.waitCount; ++i) {
        if (!p.waits[i].trigger && !p.waits[i].hasInterval) {
            fail(E_SYNTAX, "A time wait needs its interval.");
            return false;
        }
    }
    // The simulated board's _check: channels this board has, used as their
    // kind allows, the capacity, the trigger input.
    for (uint8_t i = 0; i < p.driveCount; ++i) {
        const Drive& d = p.drives[i];
        channelName(d.channel, name, sizeof name);
        if (!hasChannel(d.channel)) { fail(E_CHANNEL, "%s: this board has no such channel", name); return false; }
        if (d.channel.kind == CH_TTL) { fail(E_CHANNEL, "%s: only a DAC or a time channel is driven", name); return false; }
        if (d.tableLoop != NO_LOOP && d.tableCount < p.loops[d.tableLoop].count) {
            fail(E_RANGE, "%s: a table of %lu entries for a loop of %lu", name,
                 (unsigned long)d.tableCount, (unsigned long)p.loops[d.tableLoop].count);
            return false;
        }
        // A captured origin or restore needs the captured position, a park
        // restore the park position: the executor would fail at the first
        // write; this refuses before the run.
        if (d.origin == CAPTURED || d.restore == RESTORE_CAPTURED) {
            bool found = false;
            for (uint8_t j = 0; j < p.capturedCount; ++j) found |= sameChannel(p.captured[j].channel, d.channel);
            if (!found) { fail(E_SYNTAX, "%s: a captured origin or restore, and no CAPTURED line", name); return false; }
        }
        if (d.restore == RESTORE_PARK) {
            bool found = false;
            for (uint8_t j = 0; j < p.parkCount; ++j) found |= sameChannel(p.park[j].channel, d.channel);
            if (!found) { fail(E_SYNTAX, "%s: a park restore, and no PARK line", name); return false; }
        }
    }
    for (uint8_t i = 0; i < p.capturedCount + p.parkCount; ++i) {
        const ChannelValue& v = i < p.capturedCount ? p.captured[i] : p.park[i - p.capturedCount];
        channelName(v.channel, name, sizeof name);
        if (!hasChannel(v.channel)) { fail(E_CHANNEL, "%s: this board has no such channel", name); return false; }
        if (v.channel.kind == CH_TTL) { fail(E_CHANNEL, "%s: only a DAC or a time channel has a captured or park position", name); return false; }
    }
    for (uint8_t i = 0; i < p.eventCount; ++i) {
        channelName(p.events[i].channel, name, sizeof name);
        if (!hasChannel(p.events[i].channel)) { fail(E_CHANNEL, "%s: this board has no such channel", name); return false; }
        if (p.events[i].channel.kind == CH_TIME) { fail(E_CHANNEL, "%s: only a line or a DAC takes an event", name); return false; }
    }
    for (uint8_t i = 0; i < p.safeCount; ++i) {
        channelName(p.safe[i].channel, name, sizeof name);
        if (!hasChannel(p.safe[i].channel)) { fail(E_CHANNEL, "%s: this board has no such channel", name); return false; }
        if (p.safe[i].channel.kind == CH_TIME) { fail(E_CHANNEL, "%s: only a line or a DAC has a safe level", name); return false; }
    }
    for (uint8_t i = 0; i < p.prepareCount; ++i) {
        channelName(p.prepare[i].channel, name, sizeof name);
        if (!hasChannel(p.prepare[i].channel)) { fail(E_CHANNEL, "%s: this board has no such channel", name); return false; }
    }
    uint64_t steps = 1;
    for (uint8_t i = 0; i < p.loopCount; ++i) {
        steps *= p.loops[i].count;
        if (steps > info_.maxSteps) {
            fail(E_CAPACITY, "more than %lu steps; this board holds %lu",
                 (unsigned long)info_.maxSteps, (unsigned long)info_.maxSteps);
            return false;
        }
    }
    p.steps = (uint32_t)steps;
    for (uint8_t i = 0; i < p.waitCount; ++i) {
        if (p.waits[i].trigger && !info_.triggerIn) {
            fail(E_CAPACITY, "a trigger wait, and this board has no trigger input");
            return false;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// Board: the loop counters (StepExecutor.visits, walked incrementally)
// ---------------------------------------------------------------------------

void Board::startPass(Counters& c, uint8_t depth) const {
    // Entering a loop: its direction for this pass is decided from the
    // ordinals of its parity loops, their flat index outermost first.
    const Loop& loop = program_.loops[depth];
    uint64_t flat = 0;
    for (uint8_t i = 0; i < loop.parityCount; ++i) {
        uint8_t p = loop.parity[i];
        flat = flat * program_.loops[p].count + c.ordinal[p];
    }
    c.reversed[depth] = loop.traversal == REVERSE || (loop.traversal == SERPENTINE && (flat & 1));
    c.ordinal[depth] = 0;
    c.counter[depth] = c.reversed[depth] ? loop.count - 1 : 0;
}

void Board::firstVisit(Counters& c) const {
    memset(&c, 0, sizeof c);
    for (uint8_t d = 0; d < program_.loopCount; ++d) startPass(c, d);
}

bool Board::nextVisit(Counters& c) const {
    for (int d = program_.loopCount - 1; d >= 0; --d) {
        const Loop& loop = program_.loops[d];
        if (c.ordinal[d] + 1 < loop.count) {
            ++c.ordinal[d];
            c.counter[d] = c.reversed[d] ? loop.count - 1 - c.ordinal[d] : c.ordinal[d];
            for (uint8_t inner = d + 1; inner < program_.loopCount; ++inner) startPass(c, inner);
            return true;
        }
    }
    return false;
}

bool Board::outerDiffers(const Counters& a, const Counters& b, uint8_t depth) const {
    // Whether a loop outer to `depth` (strictly) has another counter.
    for (uint8_t d = 0; d < depth; ++d) if (a.counter[d] != b.counter[d]) return true;
    return false;
}

// ---------------------------------------------------------------------------
// Board: a run
// ---------------------------------------------------------------------------

int64_t Board::nowRel() const {
    return (int64_t)(hal_.now() - runStart_);
}

double Board::position(const Drive& d, const uint32_t* counters) const {
    double value = d.offset;
    for (uint8_t l = 0; l < program_.loopCount; ++l)
        if (d.hasWeight[l]) value += d.weight[l] * counters[l];
    if (d.tableLoop != NO_LOOP)
        value += program_.table[d.tableStart + counters[d.tableLoop]];
    if (d.origin == CAPTURED) {
        for (uint8_t i = 0; i < program_.capturedCount; ++i)
            if (sameChannel(program_.captured[i].channel, d.channel)) value += program_.captured[i].value;
    }
    return value;
}

double Board::settable(const Channel& c, double value) const {
    // What a DAC really sets: the nearest of its codes (BoardCaps.dac_value).
    if (c.kind != CH_DAC || info_.dacBits[c.index] == 0) return value;
    // The same operations in the same order as the host's, and a tie
    // rounded to even as Python's round() does, so both land on one double.
    double low = info_.dacLow[c.index], high = info_.dacHigh[c.index];
    double step = (high - low) / (pow(2.0, info_.dacBits[c.index]) - 1);
    return low + rint((value - low) / step) * step;
}

uint8_t Board::heldIndex(const Channel& c) const {
    // (executor) held: what each drive was last written. Only drives are
    // written this way; the spare slot is for a channel that is not one.
    for (uint8_t i = 0; i < program_.driveCount; ++i)
        if (sameChannel(program_.drives[i].channel, c)) return i;
    return MAX_DRIVES;
}

void Board::write(const Channel& c, double value, bool hasTick, int64_t tick) {
    // (executor) write(): a write at a tick inside the current span names
    // its tick; it never moves the board's clocks.
    value = settable(c, value);
    uint8_t h = heldIndex(c);
    if (hasHeld_[h] && held_[h] == value) return;
    hasHeld_[h] = true;
    held_[h] = value;
    if (c.kind == CH_TIME) return;      // timing only: nothing is written
    LogEntry e;
    e.kind = LOG_DAC;
    e.channel = c;
    e.segment = segment_;
    e.tick = hasTick ? tick : tick_;
    e.u.value = value;
    log_.add(e);
    schedule(wallOf(e.tick), c, value);
}

void Board::edge(const Event& ev, int64_t start, int64_t end) {
    LogEntry e;
    e.kind = LOG_ON;
    e.channel = ev.channel;
    e.segment = segment_;
    e.tick = start;
    e.u.value = ev.on;
    log_.add(e);
    e.kind = LOG_OFF;
    e.tick = end;
    e.u.value = ev.off;
    log_.add(e);
    schedule(wallOf(start), ev.channel, ev.on);
    schedule(wallOf(end), ev.channel, ev.off);
}

bool Board::schedule(int64_t wall, const Channel& c, double value) {
    // Outputs are kept in time order; equal times keep their order of
    // scheduling, which is the executor's order of logging. A time already
    // past (an edge timed before its step) is now.
    if (scheduled_ >= MAX_SCHEDULE) {
        runFailed("more than %u outputs timed ahead of the clock", (unsigned)MAX_SCHEDULE);
        return false;
    }
    int64_t now = nowRel();
    if (wall < now) wall = now;
    uint16_t i = scheduled_;
    while (i > 0 && schedule_[i - 1].wall > wall) {
        schedule_[i] = schedule_[i - 1];
        --i;
    }
    schedule_[i].wall = wall;
    schedule_[i].channel = c;
    schedule_[i].value = value;
    ++scheduled_;
    return true;
}

void Board::passTime(int64_t untilRel, const char* heartbeatKind) {
    uint64_t target = runStart_ + (uint64_t)(untilRel < 0 ? 0 : untilRel);
    while (hal_.now() < target) {
        hal_.sleepUntil(target);
        poll();
        heartbeat(heartbeatKind);
    }
}

void Board::drain(int64_t untilWall) {
    // Every output due by `untilWall`, each at its time.
    while (scheduled_ > 0 && schedule_[0].wall <= untilWall) {
        Scheduled s = schedule_[0];
        passTime(s.wall, "ALIVE");
        if (s.channel.kind == CH_DAC) hal_.writeDac(s.channel.index, s.value);
        else if (s.channel.kind == CH_TTL) hal_.writeTtl(s.channel.index, (uint8_t)s.value);
        memmove(&schedule_[0], &schedule_[1], (scheduled_ - 1) * sizeof(Scheduled));
        --scheduled_;
    }
}

void Board::advanceTo(int64_t tick) {
    // (executor) _advance(): the clocks move to `tick`; here time passes too.
    int64_t target = wallOf(tick);
    wall_ = target;
    tick_ = tick;
    drain(target);
    passTime(target, "ALIVE");
}

void Board::poll() {
    // The link while a run goes on: a STOP is honoured where the executor
    // honours it; the rest is answered as the simulated board answers.
    int byte;
    while ((byte = hal_.readByte()) >= 0) feed(byte);
}

void Board::heartbeat(const char* kind) {
    if (!info_.hasAlive) return;
    if (hal_.now() - lastMessageAt_ >= info_.aliveTicks)
        replyf("%s,%lu", kind, (unsigned long)segment_);
}

bool Board::runFailed(const char* fmt, ...) {
    if (failure_[0] == 0) {
        va_list args;
        va_start(args, fmt);
        vsnprintf(failure_, sizeof failure_, fmt, args);
        va_end(args);
    }
    return false;
}

bool Board::stopped() {
    poll();
    return stop_;
}

double Board::restoreTarget(const Drive& d, const double* first, const double* here) const {
    uint8_t i = heldIndex(d.channel);
    if (d.restore == RESTORE_CAPTURED) {
        for (uint8_t j = 0; j < program_.capturedCount; ++j)
            if (sameChannel(program_.captured[j].channel, d.channel)) return program_.captured[j].value;
    }
    if (d.restore == RESTORE_FIRST) return first[i];
    if (d.restore == RESTORE_PARK) {
        for (uint8_t j = 0; j < program_.parkCount; ++j)
            if (sameChannel(program_.park[j].channel, d.channel)) return program_.park[j].value;
    }
    return here[i];
}

void Board::safeState() {
    // Every channel with a SAFE level at it; every other DAC holds.
    for (uint8_t i = 0; i < program_.safeCount; ++i) {
        const ChannelValue& s = program_.safe[i];
        if (s.channel.kind == CH_DAC) hal_.writeDac(s.channel.index, settable(s.channel, s.value));
        else if (s.channel.kind == CH_TTL) hal_.writeTtl(s.channel.index, (uint8_t)s.value);
    }
}

void Board::run() {
    stop_ = false;
    log_.clear();
    failure_[0] = 0;
    scheduled_ = 0;
    memset(hasHeld_, 0, sizeof hasHeld_);
    segment_ = 0;
    step_ = 0;
    reply("OK,RUN");
    running_ = true;
    runStart_ = hal_.now();
    wall_ = tick_ = 0;
    lastStepReportAt_ = 0;
    reply("STARTED");
    bool finished = walk();
    // Whatever is still timed ahead of the clock (an event's edge past the
    // last step) happens before the outcome is reported; a stop or a
    // failure drops it: the safe state is what those leave.
    if (finished) {
        while (scheduled_ > 0) drain(schedule_[scheduled_ - 1].wall);
    }
    scheduled_ = 0;
    safeState();
    running_ = false;
    LogEntry e;
    e.channel = timeChannel();
    e.segment = segment_;
    e.tick = wall_;
    e.u.clock = 0;
    if (finished) {
        replyf("DONE,%lld", (long long)wall_);
    } else if (failure_[0]) {
        e.kind = LOG_FAILED;
        log_.add(e);
        log_.setFailure(failure_);
        replyf("FAILED,%lu,%s", (unsigned long)step_, failure_);
    } else {
        e.kind = LOG_SAFE;
        log_.add(e);
        replyf("STOPPED,%lu,%lld", (unsigned long)step_, (long long)wall_);
    }
}

bool Board::wait(const Wait& w, const double* here) {
    // (executor) _wait()
    if (!w.trigger) {
        advanceTo(tick_ + w.interval);
        return true;
    }
    int64_t start = wall_;
    if (stopped()) return false;
    uint64_t at = 0;
    int64_t release;
    bool arrived = false;
    uint64_t startAbs = runStart_ + (uint64_t)start;
    uint64_t deadlineAbs = startAbs + w.timeout;
    while (true) {
        Hal::Arrival a = hal_.waitArrival(startAbs, w.hasTimeout, deadlineAbs, &at);
        if (a == Hal::ARRIVED) {
            arrived = true;
            release = (int64_t)(at - runStart_) + (info_.hasLatency ? info_.latencyTicks : 0);
            passTime(release, "WAITING");
            int64_t now = nowRel();
            if (now > release) release = now;
            break;
        }
        if (a == Hal::TIMED_OUT) {
            if (w.failOnTimeout) return runFailed("no trigger within %lu ticks", (unsigned long)w.timeout);
            release = start + w.timeout;
            break;
        }
        if (stopped()) return false;
        heartbeat("WAITING");
    }
    // A release starts a new segment.
    wall_ = release;
    ++segment_;
    tick_ = 0;
    LogEntry e;
    e.kind = LOG_RELEASE;
    e.channel = timeChannel();
    e.segment = segment_;
    e.tick = release;
    e.u.clock = arrived ? (int64_t)(at - runStart_) : NO_ARRIVAL;
    log_.add(e);
    replyf("RELEASE,%lu,%lld", (unsigned long)segment_, (long long)release);
    // Preparation: only what needs it. Drives whose held value is not this
    // step's are written now; the board waits the longest move + settle of
    // those and every PREPARE time.
    int64_t need = 0;
    for (uint8_t i = 0; i < program_.driveCount; ++i) {
        const Drive& d = program_.drives[i];
        double target = settable(d.channel, here[i]);
        if (!hasHeld_[i] || held_[i] != target) {
            write(d.channel, here[i], false, 0);
            int64_t t = (int64_t)d.move + d.settle;
            if (t > need) need = t;
        }
    }
    for (uint8_t i = 0; i < program_.prepareCount; ++i)
        if ((int64_t)program_.prepare[i].ticks > need) need = program_.prepare[i].ticks;
    advanceTo(need);
    replyf("PREPARED,%lu,%lld", (unsigned long)segment_, (long long)wall_);
    return true;
}

bool Board::walk() {
    const Program& p = program_;
    const uint8_t L = p.loopCount;
    const uint8_t innermost = L - 1;
    Counters prev, cur, next;
    double first[MAX_DRIVES], posCur[MAX_DRIVES], posNext[MAX_DRIVES];
    memset(posNext, 0, sizeof posNext);

    firstVisit(cur);
    prev = cur;
    next = cur;
    bool hasNext = nextVisit(next);
    for (uint8_t i = 0; i < p.driveCount; ++i) {
        first[i] = posCur[i] = position(p.drives[i], cur.counter);
        if (hasNext) posNext[i] = position(p.drives[i], next.counter);
    }
    // (executor) every drive at its first value before the first step.
    for (uint8_t i = 0; i < p.driveCount; ++i) write(p.drives[i].channel, first[i], false, 0);
    if (failure_[0]) return false;

    for (uint32_t index = 0; ; ++index) {
        step_ = index;
        if (failEnabled_ && index == failStep_) return runFailed("injected failure at step %lu", (unsigned long)index);
        if (stopped()) return false;
        uint64_t now = hal_.now();
        if (index == 0 || now - lastStepReportAt_ >= info_.stepReportTicks) {
            replyf("STEP,%lu,%lu", (unsigned long)index, (unsigned long)p.steps);
            lastStepReportAt_ = now;
        }
        uint32_t phase = p.phaseLoop == NO_LOOP ? 0 : cur.counter[p.phaseLoop];

        // The waits here: at the start (the first step), each loop's at the
        // first step of its pass (a change in it or a loop outside it),
        // outermost first, then the step's. (executor) _waits_here.
        for (uint8_t i = 0; i < p.waitCount; ++i)
            if (p.waits[i].at == WAIT_START && index == 0 && !wait(p.waits[i], posCur)) return false;
        for (uint8_t depth = 0; depth < L; ++depth) {
            bool starts = index == 0 || outerDiffers(cur, prev, depth + 1);
            if (!starts) continue;
            for (uint8_t i = 0; i < p.waitCount; ++i)
                if (p.waits[i].at == depth && !wait(p.waits[i], posCur)) return false;
        }
        for (uint8_t i = 0; i < p.waitCount; ++i)
            if (p.waits[i].at == WAIT_STEP && !wait(p.waits[i], posCur)) return false;

        int64_t stepStart = tick_;
        bool last = !hasNext;
        bool passEnd = last || outerDiffers(cur, next, innermost);

        for (uint8_t i = 0; i < p.eventCount; ++i) {
            const Event& e = p.events[i];
            if (!e.allPhases) {
                bool inPhase = false;
                for (uint8_t j = 0; j < e.phaseCount; ++j) inPhase |= e.phases[j] == phase;
                if (!inPhase) continue;
            }
            if (e.anchor == AT_STEP) {
                edge(e, stepStart + e.start, stepStart + e.end);
            } else if (e.anchor == PASS_START) {
                bool starts = index == 0 || outerDiffers(cur, prev, (uint8_t)e.anchorLoop);
                if (starts) edge(e, stepStart + e.start, stepStart + e.end);
            } else {
                bool ends = last || outerDiffers(cur, next, (uint8_t)e.anchorLoop);
                if (ends) {
                    int64_t end = stepStart + p.stepTicks;
                    edge(e, end + e.start, end + e.end);
                }
            }
        }
        if (failure_[0]) return false;

        if (!passEnd) {
            for (uint8_t i = 0; i < p.driveCount; ++i) {
                const Drive& d = p.drives[i];
                if (d.placement == IN_STEP && posNext[i] != posCur[i])
                    write(d.channel, posNext[i], true,
                          stepStart + (int64_t)p.stepTicks - (int64_t)d.move - (int64_t)d.settle);
            }
            advanceTo(stepStart + p.stepTicks);
            int64_t longest = 0;
            bool movers = false;
            for (uint8_t i = 0; i < p.driveCount; ++i) {
                const Drive& d = p.drives[i];
                if (d.placement == BETWEEN_STEPS && posNext[i] != posCur[i]) {
                    movers = true;
                    write(d.channel, posNext[i], false, 0);
                    int64_t t = (int64_t)d.move + d.settle;
                    if (t > longest) longest = t;
                }
            }
            if (movers) advanceTo(tick_ + longest);
        } else {
            advanceTo(stepStart + p.stepTicks);
            for (uint8_t i = 0; i < p.driveCount; ++i) {
                double target = last ? restoreTarget(p.drives[i], first, posCur) : posNext[i];
                write(p.drives[i].channel, target, false, 0);
            }
            advanceTo(tick_ + p.returnTicks);
        }
        if (failure_[0]) return false;
        if (last) break;

        prev = cur;
        cur = next;
        hasNext = nextVisit(next);
        for (uint8_t i = 0; i < p.driveCount; ++i) {
            posCur[i] = posNext[i];
            if (hasNext) posNext[i] = position(p.drives[i], next.counter);
        }
    }
    step_ = p.steps;
    LogEntry e;
    e.kind = LOG_FINISH;
    e.channel = timeChannel();
    e.segment = segment_;
    e.tick = tick_;
    e.u.clock = wall_;
    log_.add(e);
    return true;
}

}  // namespace stepfw

// Copyright (C) 2026 ImSwitch developers
// This file is part of ImSwitch.
//
// ImSwitch is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// ImSwitch is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.
