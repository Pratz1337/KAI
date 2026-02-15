#include <ntddk.h>
#include <wdf.h>

#include "Public.h"

EVT_WDF_IO_QUEUE_IO_DEVICE_CONTROL AikEvtIoDeviceControl;

static SIZE_T AikStrLen(_In_ const char* Str)
{
    SIZE_T n = 0;
    while (Str && Str[n] != '\0')
        n++;
    return n;
}

static VOID AikCompleteWithString(_In_ WDFREQUEST Request, _In_ const char* Str)
{
    size_t outLen = 0;
    PVOID outBuf = NULL;
    NTSTATUS status = WdfRequestRetrieveOutputBuffer(Request, 1, &outBuf, &outLen);
    if (NT_SUCCESS(status) && outLen > 0)
    {
        size_t n = (size_t)AikStrLen(Str);
        if (n + 1 > outLen)
            n = outLen - 1;
        RtlCopyMemory(outBuf, Str, n);
        ((char*)outBuf)[n] = '\0';
        WdfRequestSetInformation(Request, (ULONG_PTR)(n + 1));
        WdfRequestComplete(Request, STATUS_SUCCESS);
        return;
    }

    WdfRequestComplete(Request, STATUS_SUCCESS);
}

NTSTATUS AikQueueInitialize(_In_ WDFDEVICE Device)
{
    WDF_IO_QUEUE_CONFIG queueConfig;
    NTSTATUS status;
    WDFQUEUE queue;

    WDF_IO_QUEUE_CONFIG_INIT_DEFAULT_QUEUE(&queueConfig, WdfIoQueueDispatchSequential);
    queueConfig.EvtIoDeviceControl = AikEvtIoDeviceControl;

    status = WdfIoQueueCreate(Device, &queueConfig, WDF_NO_OBJECT_ATTRIBUTES, &queue);
    if (!NT_SUCCESS(status))
    {
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_ERROR_LEVEL, "AIK: WdfIoQueueCreate failed: 0x%08X\n", status));
        return status;
    }

    return STATUS_SUCCESS;
}

VOID AikEvtIoDeviceControl(
    _In_ WDFQUEUE Queue,
    _In_ WDFREQUEST Request,
    _In_ size_t OutputBufferLength,
    _In_ size_t InputBufferLength,
    _In_ ULONG IoControlCode
)
{
    UNREFERENCED_PARAMETER(Queue);
    UNREFERENCED_PARAMETER(OutputBufferLength);
    UNREFERENCED_PARAMETER(InputBufferLength);

    switch (IoControlCode)
    {
    case IOCTL_AIK_PING:
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_INFO_LEVEL, "AIK: IOCTL_AIK_PING\n"));
        AikCompleteWithString(Request, "PONG");
        return;

    case IOCTL_AIK_ECHO:
    {
        PVOID inBuf = NULL;
        size_t inLen = 0;
        PVOID outBuf = NULL;
        size_t outLen = 0;
        NTSTATUS status;

        status = WdfRequestRetrieveInputBuffer(Request, 1, &inBuf, &inLen);
        if (!NT_SUCCESS(status))
        {
            WdfRequestComplete(Request, status);
            return;
        }

        status = WdfRequestRetrieveOutputBuffer(Request, 1, &outBuf, &outLen);
        if (!NT_SUCCESS(status))
        {
            WdfRequestComplete(Request, status);
            return;
        }

        if (outLen < inLen)
            inLen = outLen;

        RtlCopyMemory(outBuf, inBuf, inLen);
        WdfRequestSetInformation(Request, (ULONG_PTR)inLen);
        WdfRequestComplete(Request, STATUS_SUCCESS);
        return;
    }

    default:
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_WARNING_LEVEL, "AIK: Unknown IOCTL: 0x%08X\n", IoControlCode));
        WdfRequestComplete(Request, STATUS_INVALID_DEVICE_REQUEST);
        return;
    }
}
