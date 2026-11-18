/*
  The NUCLEO-H7A3ZI-Q as an IMSWITCH_STEP/1 step board: what the CubeMX
  project calls (main.c, usbd_cdc_if.c). See ../README.md.

  Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.
*/

#ifndef NUCLEO_STEP_H
#define NUCLEO_STEP_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The board: from main(), after CubeMX's initialisation. Never returns. */
void nucleo_step_main(void);

/* From the USB interrupt (CDC_Receive_HS): bytes that arrived. Returns 1 when
   reception may be armed again at once, 0 when there is no room for another
   packet yet; the board then arms it itself (hw_usb_rearm) once there is. */
int nucleo_step_usb_received(const uint8_t* data, uint32_t length);

/* From the USB interrupt (CDC_TransmitCplt_HS): the last transfer is done. */
void nucleo_step_usb_sent(void);

#ifdef __cplusplus
}
#endif

#endif
