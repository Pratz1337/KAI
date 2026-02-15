#pragma once

#include <wdm.h>

// Device names used by the stub.
// User-mode open path: \\\\.\\AikKmdfIoctl
#define AIK_DEVICE_NAME      L"\\Device\\AikKmdfIoctl"
#define AIK_DOS_DEVICE_NAME  L"\\DosDevices\\AikKmdfIoctl"

// IOCTL interface (buffered).
#define AIK_IOCTL_INDEX 0x800

#define IOCTL_AIK_PING CTL_CODE(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 0, METHOD_BUFFERED, FILE_ANY_ACCESS)
#define IOCTL_AIK_ECHO CTL_CODE(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 1, METHOD_BUFFERED, FILE_ANY_ACCESS)
#define IOCTL_AIK_INJECT_SCANCODE CTL_CODE(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 2, METHOD_BUFFERED, FILE_ANY_ACCESS)
#define IOCTL_AIK_INJECT_SCANCODES CTL_CODE(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 3, METHOD_BUFFERED, FILE_ANY_ACCESS)

// Maximum scancodes per batch injection
#define AIK_MAX_SCANCODES 64

// Scancode input structure (matches user-mode)
#pragma pack(push, 1)
typedef struct _AIK_SCANCODE_INPUT {
    USHORT ScanCode;    // Hardware scancode
    UCHAR  Flags;       // 0 = key down, 1 = key up, 2 = extended key
} AIK_SCANCODE_INPUT, *PAIK_SCANCODE_INPUT;

typedef struct _AIK_SCANCODE_BATCH {
    ULONG Count;                                // Number of scancodes
    AIK_SCANCODE_INPUT Scancodes[AIK_MAX_SCANCODES];
} AIK_SCANCODE_BATCH, *PAIK_SCANCODE_BATCH;
#pragma pack(pop)

// Scancode flags
#define AIK_KEY_DOWN     0x00
#define AIK_KEY_UP       0x01
#define AIK_KEY_EXTENDED 0x02

