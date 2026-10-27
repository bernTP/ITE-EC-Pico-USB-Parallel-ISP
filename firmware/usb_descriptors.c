/**
 * @file usb_descriptors.c
 *
 * USB descriptors: one vendor-class interface with a bulk endpoint pair.
 * Based on the I2C-Pico-USB descriptors (Daniel Quadros, MIT).
 */

#include "tusb.h"
#include "pico/unique_id.h"

#include "hwconfig.h"

#define USBD_STR_0 (0x00)
#define USBD_STR_MANUF (0x01)
#define USBD_STR_PRODUCT (0x02)
#define USBD_STR_SERIAL (0x03)

#define EPNUM_VENDOR_OUT 0x01
#define EPNUM_VENDOR_IN  0x81

//--------------------------------------------------------------------+
// Device Descriptors
//--------------------------------------------------------------------+
static const tusb_desc_device_t usbd_desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = 0x00,
    .bDeviceSubClass = 0x00,
    .bDeviceProtocol = 0x00,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USBD_VID,
    .idProduct = USBD_PID,
    .bcdDevice = USBD_DEVICE,
    .iManufacturer = USBD_STR_MANUF,
    .iProduct = USBD_STR_PRODUCT,
    .iSerialNumber = USBD_STR_SERIAL,
    .bNumConfigurations = 1,
};

//--------------------------------------------------------------------+
// Configuration Descriptor
//--------------------------------------------------------------------+

#define CONFIG_TOTAL_LEN  (TUD_CONFIG_DESC_LEN + TUD_VENDOR_DESC_LEN)

static uint8_t const desc_configuration[] =
{
  // Config number, interface count, string index, total length, attribute, power in mA
  TUD_CONFIG_DESCRIPTOR(1, 1, 0, CONFIG_TOTAL_LEN, 0, 100),
  // Interface number, string index, EP Out & IN address, EP size
  TUD_VENDOR_DESCRIPTOR(0, USBD_STR_0, EPNUM_VENDOR_OUT, EPNUM_VENDOR_IN,
                        CFG_TUD_VENDOR_EPSIZE)
};

static char usbd_serial_str[PICO_UNIQUE_BOARD_ID_SIZE_BYTES * 2 + 1];

//--------------------------------------------------------------------+
// String Descriptors
//--------------------------------------------------------------------+

static const char *const usbd_desc_str[] = {
    [USBD_STR_MANUF] = "ITE-EC-Pico-ISP",
    [USBD_STR_PRODUCT] = "ite-pp-isp",
    [USBD_STR_SERIAL] = usbd_serial_str,
};


// Invoked when received GET DEVICE DESCRIPTOR
const uint8_t *tud_descriptor_device_cb(void) {
    return (const uint8_t *)&usbd_desc_device;
}

// Invoked when received GET CONFIGURATION DESCRIPTOR
const uint8_t *tud_descriptor_configuration_cb(__unused uint8_t index) {
    return desc_configuration;
}

// Invoked when received GET STRING DESCRIPTOR request
const uint16_t *tud_descriptor_string_cb(uint8_t index, __unused uint16_t langid) {
    #define DESC_STR_MAX (20)
    static uint16_t desc_str[DESC_STR_MAX];

    // Assign the SN using the unique flash id
    if (!usbd_serial_str[0]) {
        pico_get_unique_board_id_string(usbd_serial_str, sizeof(usbd_serial_str));
    }

    uint8_t len;
    if (index == 0) {
        desc_str[1] = 0x0409; // supported language is English
        len = 1;
    } else {
        if (index >= sizeof(usbd_desc_str) / sizeof(usbd_desc_str[0])) {
            return NULL;
        }
        const char *str = usbd_desc_str[index];
        for (len = 0; len < DESC_STR_MAX - 1 && str[len]; ++len) {
            desc_str[1 + len] = str[len];
        }
    }

    // first byte is length (including header), second byte is string type
    desc_str[0] = (uint16_t) ((TUSB_DESC_STRING << 8) | (2 * len + 2));

    return desc_str;
}
