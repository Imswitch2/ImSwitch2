/*
  What the Nucleo's board layer (nucleo_step.cpp) needs from the hardware.

  nucleo_hw_stm32.c implements it on the NUCLEO-H7A3ZI-Q; host/nucleo_host.cpp
  fakes it on a PC, which is how the board layer and the core are tested
  without a board. Everything above this line is the same code on both.

  Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.
*/

#ifndef NUCLEO_HW_H
#define NUCLEO_HW_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* After CubeMX's own initialisation: the cycle counter, the DACs written
   directly (no trigger), the lines low, the galvos at their boot position. */
void hw_init(void);

/* Microseconds since hw_init, on a 64-bit count that never wraps. Called
   from the main loop only. */
uint64_t hw_micros(void);

/* DAC channel 0 (PA4, galvo X) or 1 (PA5, galvo Y): a 12-bit code, now. */
void hw_dac(uint8_t channel, uint16_t code);

/* TTL line 0, 1 or 2 (PA0, PA1, PA2): 0 or 1, now. */
void hw_ttl(uint8_t line, uint8_t level);

/* Start sending `length` bytes over USB. 0: the transfer started, and the
   USB interrupt calls nucleo_step_usb_sent() when it is done; the bytes must
   stay put until then. Anything else: not now (busy, or not connected). */
int hw_usb_send(const uint8_t* data, uint16_t length);

/* Mask (0) or unmask (1) the USB interrupt around a hand-over of the
   transmit queue between the main loop and the interrupt. */
void hw_usb_irq(int enable);

/* Arm USB reception again after nucleo_step_usb_received() returned 0 (no
   room for another packet): until then the host is held off. */
void hw_usb_rearm(void);

/* Between bytes while no program runs. */
void hw_idle(void);

/* The red LED (LD3): on while the running program has had an output written
   late enough to mean the board cannot keep up with its steps. */
void hw_late_led(int on);

#ifdef __cplusplus
}
#endif

#endif
