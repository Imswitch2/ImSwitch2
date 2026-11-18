/*
  nucleo_hw.h on the NUCLEO-H7A3ZI-Q (STM32H7A3ZIT6Q), in the STM32CubeIDE
  project made from trigger_generator (../README.md says how).

  - Time: the Cortex-M7 cycle counter (DWT), extended to 64 bits and divided
    into microseconds. It follows SystemCoreClock, so it is as accurate as
    the clock the project runs on: the internal RC oscillator by default, the
    ST-LINK's 8 MHz (HSE) when the project is set to it.
  - DACs: DAC1 channels 1 and 2 (PA4, PA5), configured here with no trigger
    and written to their data registers, so a code is out within a bus clock.
    CubeMX's own configuration (the old firmware's TIM6 trigger) is replaced.
  - Lines: PA0-PA2 through GPIOA's set/reset register, one write per change.
  - USB: CDC_Transmit_HS, and the USB interrupt masked in the NVIC while the
    main loop hands the transmit queue over.

  Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.
*/

#include "main.h"
#include "usbd_cdc_if.h"

#include "nucleo_hw.h"

extern DAC_HandleTypeDef hdac1;
extern USBD_HandleTypeDef hUsbDeviceHS;

/* In usbd_cdc_if.c, USER CODE BEGIN PRIVATE_FUNCTIONS_IMPLEMENTATION: arms
   reception with the CDC's own receive buffer, which is private to that file. */
void cdc_rearm(void);

/* Where the galvos stand from power-up until the host sets them: the old
   firmware's origins, so the rig looks as it did before. */
static const float BOOT_VOLTS[2] = {1.2f, 1.6f};

static const uint32_t TTL_PINS[3] = {GPIO_PIN_0, GPIO_PIN_1, GPIO_PIN_2};

static uint32_t cyclesPerMicro = 64;
static uint64_t cycleBase = 0;
static uint32_t cycleLast = 0;

static void dacDirect(uint32_t channel) {
    DAC_ChannelConfTypeDef config = {0};
    config.DAC_SampleAndHold = DAC_SAMPLEANDHOLD_DISABLE;
    config.DAC_Trigger = DAC_TRIGGER_NONE;
    config.DAC_OutputBuffer = DAC_OUTPUTBUFFER_ENABLE;
    /* The old firmware's setting (its legacy name DAC_CHIPCONNECT_ENABLE): with
       the buffer on, the output reaches the pin and the on-chip peripherals. */
    config.DAC_ConnectOnChipPeripheral = DAC_CHIPCONNECT_INTERNAL;
    config.DAC_UserTrimming = DAC_TRIMMING_FACTORY;
    HAL_DAC_Stop(&hdac1, channel);
    if (HAL_DAC_ConfigChannel(&hdac1, &config, channel) != HAL_OK) Error_Handler();
    if (HAL_DAC_Start(&hdac1, channel) != HAL_OK) Error_Handler();
}

void hw_init(void) {
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->LAR = 0xC5ACCE55;                  /* the Cortex-M7's DWT is locked at reset */
    DWT->CYCCNT = 0;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    cyclesPerMicro = SystemCoreClock / 1000000u;
    if (cyclesPerMicro == 0) cyclesPerMicro = 1;

    dacDirect(DAC_CHANNEL_1);
    dacDirect(DAC_CHANNEL_2);
    for (uint8_t i = 0; i < 2; ++i) {
        float code = BOOT_VOLTS[i] / 3.3f * 4095.0f + 0.5f;
        hw_dac(i, (uint16_t)code);
    }
    for (uint8_t line = 0; line < 3; ++line) hw_ttl(line, 0);
}

uint64_t hw_micros(void) {
    /* The counter wraps every 2^32 cycles (15 s at 280 MHz); the main loop
       looks far more often than that. */
    uint32_t now = DWT->CYCCNT;
    if (now < cycleLast) cycleBase += (uint64_t)1 << 32;
    cycleLast = now;
    return (cycleBase + now) / cyclesPerMicro;
}

void hw_dac(uint8_t channel, uint16_t code) {
    if (code > 4095u) code = 4095u;
    if (channel == 0) DAC1->DHR12R1 = code;
    else if (channel == 1) DAC1->DHR12R2 = code;
}

void hw_ttl(uint8_t line, uint8_t level) {
    if (line > 2) return;
    GPIOA->BSRR = level ? TTL_PINS[line] : (TTL_PINS[line] << 16);
}

int hw_usb_send(const uint8_t* data, uint16_t length) {
    if (hUsbDeviceHS.dev_state != USBD_STATE_CONFIGURED || hUsbDeviceHS.pClassData == NULL)
        return -1;
    return CDC_Transmit_HS((uint8_t*)data, length) == USBD_OK ? 0 : -1;
}

void hw_usb_irq(int enable) {
    if (enable) HAL_NVIC_EnableIRQ(OTG_HS_IRQn);
    else HAL_NVIC_DisableIRQ(OTG_HS_IRQn);
}

void hw_usb_rearm(void) {
    hw_usb_irq(0);
    cdc_rearm();
    hw_usb_irq(1);
}

void hw_idle(void) {
}

void hw_late_led(int on) {
    HAL_GPIO_WritePin(LD3_GPIO_Port, LD3_Pin, on ? GPIO_PIN_SET : GPIO_PIN_RESET);
}
