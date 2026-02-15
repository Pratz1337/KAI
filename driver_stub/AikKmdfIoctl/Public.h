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

