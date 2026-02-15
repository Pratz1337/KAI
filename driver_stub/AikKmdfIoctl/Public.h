#pragma once

#include <wdm.h>

// Device names used by the stub.
// User-mode open path: \\\\.\\AikKmdfIoctl
#define AIK_DEVICE_NAME      L"\\Device\\AikKmdfIoctl"
#define AIK_DOS_DEVICE_NAME  L"\\DosDevices\\AikKmdfIoctl"

// IOCTL interface (buffered).
#define AIK_IOCTL_INDEX 0x800

#define IOCTL_AIK_PING       CTL_CODE(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 0, METHOD_BUFFERED, FILE_ANY_ACCESS)
#define IOCTL_AIK_ECHO       CTL_CODE(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 1, METHOD_BUFFERED, FILE_ANY_ACCESS)
#define IOCTL_AIK_INJECT_KEY CTL_CODE(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 2, METHOD_BUFFERED, FILE_ANY_ACCESS)

// ------------------------------------------------------------------
// Scancode injection payload sent from user-mode via IOCTL_AIK_INJECT_KEY.
//
// The buffer is an AIK_KEY_PACKET header followed by `Count` entries of
// AIK_SCANCODE.  Maximum Count = AIK_MAX_SCANCODES (32).
// ------------------------------------------------------------------

#define AIK_MAX_SCANCODES 32

// Flags (match Windows KEYBOARD_INPUT_DATA.Flags bit-field).
#define AIK_KEY_MAKE    0   // key down
#define AIK_KEY_BREAK   1   // key up
#define AIK_KEY_E0      2   // extended scancode prefix E0
#define AIK_KEY_E1      4   // extended scancode prefix E1

#pragma pack(push, 1)

typedef struct _AIK_SCANCODE {
    USHORT MakeCode;   // PS/2 set-1 scancode
    USHORT Flags;      // AIK_KEY_MAKE | AIK_KEY_BREAK | AIK_KEY_E0 ...
} AIK_SCANCODE, *PAIK_SCANCODE;

typedef struct _AIK_KEY_PACKET {
    ULONG Count;                          // number of entries that follow
    AIK_SCANCODE Codes[AIK_MAX_SCANCODES]; // variable up to Count
} AIK_KEY_PACKET, *PAIK_KEY_PACKET;

#pragma pack(pop)

