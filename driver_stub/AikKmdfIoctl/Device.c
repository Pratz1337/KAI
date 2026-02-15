#include <ntddk.h>
#include <wdf.h>

#include "Public.h"

NTSTATUS AikQueueInitialize(_In_ WDFDEVICE Device);

NTSTATUS AikCreateDevice(_Inout_ PWDFDEVICE_INIT DeviceInit)
{
    WDFDEVICE device;
    NTSTATUS status;
    WDF_OBJECT_ATTRIBUTES attrs;
    UNICODE_STRING symLink;

    WdfDeviceInitSetDeviceType(DeviceInit, FILE_DEVICE_UNKNOWN);
    WdfDeviceInitSetIoType(DeviceInit, WdfDeviceIoBuffered);

    WDF_OBJECT_ATTRIBUTES_INIT(&attrs);

    status = WdfDeviceCreate(&DeviceInit, &attrs, &device);
    if (!NT_SUCCESS(status))
    {
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_ERROR_LEVEL, "AIK: WdfDeviceCreate failed: 0x%08X\n", status));
        return status;
    }

    RtlInitUnicodeString(&symLink, AIK_DOS_DEVICE_NAME);
    status = WdfDeviceCreateSymbolicLink(device, &symLink);
    if (!NT_SUCCESS(status))
    {
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_ERROR_LEVEL, "AIK: WdfDeviceCreateSymbolicLink failed: 0x%08X\n", status));
        return status;
    }

    status = AikQueueInitialize(device);
    if (!NT_SUCCESS(status))
    {
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_ERROR_LEVEL, "AIK: Queue init failed: 0x%08X\n", status));
        return status;
    }

    KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_INFO_LEVEL, "AIK: Device created: %ws\n", AIK_DOS_DEVICE_NAME));
    return STATUS_SUCCESS;
}

