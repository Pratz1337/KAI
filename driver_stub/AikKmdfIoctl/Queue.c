#include <ntddk.h>
#include <wdf.h>
#include <ntddkbd.h>

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

// Simulate scancode injection (logs only in this stub version)
// Real implementation would use KeyboardClassServiceCallback or similar
static NTSTATUS AikInjectScancodes(_In_ PAIK_SCANCODE_BATCH Batch)
{
    if (Batch == NULL || Batch->Count == 0 || Batch->Count > AIK_MAX_SCANCODES)
    {
        return STATUS_INVALID_PARAMETER;
    }

    for (ULONG i = 0; i < Batch->Count; i++)
    {
        PAIK_SCANCODE_INPUT sc = &Batch->Scancodes[i];
        const char* action = (sc->Flags & AIK_KEY_UP) ? "UP" : "DOWN";
        const char* extended = (sc->Flags & AIK_KEY_EXTENDED) ? " EXT" : "";
        
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_INFO_LEVEL, 
            "AIK: Inject scancode 0x%04X %s%s\n", 
            sc->ScanCode, action, extended));
    }

    // TODO: For real injection, you would need to:
    // 1. Attach to kbdclass driver
    // 2. Call KeyboardClassServiceCallback with KEYBOARD_INPUT_DATA
    // For hackathon demo, this stub logs the intent

    return STATUS_SUCCESS;
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

    case IOCTL_AIK_INJECT_SCANCODE:
    {
        PVOID inBuf = NULL;
        size_t inLen = 0;
        NTSTATUS status;

        status = WdfRequestRetrieveInputBuffer(Request, sizeof(AIK_SCANCODE_INPUT), &inBuf, &inLen);
        if (!NT_SUCCESS(status) || inLen < sizeof(AIK_SCANCODE_INPUT))
        {
            WdfRequestComplete(Request, STATUS_INVALID_PARAMETER);
            return;
        }

        PAIK_SCANCODE_INPUT sc = (PAIK_SCANCODE_INPUT)inBuf;
        AIK_SCANCODE_BATCH batch;
        batch.Count = 1;
        batch.Scancodes[0] = *sc;

        status = AikInjectScancodes(&batch);
        WdfRequestComplete(Request, status);
        return;
    }

    case IOCTL_AIK_INJECT_SCANCODES:
    {
        PVOID inBuf = NULL;
        size_t inLen = 0;
        NTSTATUS status;

        status = WdfRequestRetrieveInputBuffer(Request, sizeof(AIK_SCANCODE_BATCH), &inBuf, &inLen);
        if (!NT_SUCCESS(status) || inLen < sizeof(ULONG))
        {
            WdfRequestComplete(Request, STATUS_INVALID_PARAMETER);
            return;
        }

        PAIK_SCANCODE_BATCH batch = (PAIK_SCANCODE_BATCH)inBuf;
        
        // Validate batch size
        size_t expectedSize = sizeof(ULONG) + batch->Count * sizeof(AIK_SCANCODE_INPUT);
        if (inLen < expectedSize || batch->Count > AIK_MAX_SCANCODES)
        {
            WdfRequestComplete(Request, STATUS_INVALID_PARAMETER);
            return;
        }

        status = AikInjectScancodes(batch);
        WdfRequestComplete(Request, status);
        return;
    }

    default:
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_WARNING_LEVEL, "AIK: Unknown IOCTL: 0x%08X\n", IoControlCode));
        WdfRequestComplete(Request, STATUS_INVALID_DEVICE_REQUEST);
        return;
    }
}
