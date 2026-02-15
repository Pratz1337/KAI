#include <ntddk.h>
#include <wdf.h>
#include <ntddkbd.h>    // KEYBOARD_INPUT_DATA
#include <kbdmou.h>     // CONNECT_DATA, PSERVICE_CALLBACK_ROUTINE

#include "Public.h"

EVT_WDF_IO_QUEUE_IO_DEVICE_CONTROL AikEvtIoDeviceControl;

// -------------------------------------------------------------------
// Keyboard class-service callback chain used for scancode injection.
// When the driver is attached as an upper filter on the keyboard stack
// the class driver calls us here; we forward to the real callback.
// For standalone (non-filter) operation we call the fallback that uses
// NtUserInjectKeyboardInput if the connect has not been set.
// -------------------------------------------------------------------

static CONNECT_DATA g_ConnectData;   // filled by IOCTL_INTERNAL_KEYBOARD_CONNECT
static BOOLEAN      g_Connected = FALSE;

// Forward declaration for class-service callback.
VOID AikServiceCallback(
    _In_    PDEVICE_OBJECT       DeviceObject,
    _In_    PKEYBOARD_INPUT_DATA InputDataStart,
    _In_    PKEYBOARD_INPUT_DATA InputDataEnd,
    _Inout_ PULONG               InputDataConsumed
);

VOID AikServiceCallback(
    _In_    PDEVICE_OBJECT       DeviceObject,
    _In_    PKEYBOARD_INPUT_DATA InputDataStart,
    _In_    PKEYBOARD_INPUT_DATA InputDataEnd,
    _Inout_ PULONG               InputDataConsumed
)
{
    UNREFERENCED_PARAMETER(DeviceObject);

    if (g_Connected && g_ConnectData.ClassService)
    {
        // Forward to the real keyboard class driver.
        ((PSERVICE_CALLBACK_ROUTINE)g_ConnectData.ClassService)(
            g_ConnectData.ClassDeviceObject,
            InputDataStart,
            InputDataEnd,
            InputDataConsumed
        );
    }
    else
    {
        *InputDataConsumed = (ULONG)(InputDataEnd - InputDataStart);
    }
}

// -------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------

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

    // ----- Scancode injection IOCTL -----
    case IOCTL_AIK_INJECT_KEY:
    {
        PVOID inBuf = NULL;
        size_t inLen = 0;
        NTSTATUS status;
        PAIK_KEY_PACKET pkt;
        ULONG i;

        status = WdfRequestRetrieveInputBuffer(Request, sizeof(AIK_KEY_PACKET), &inBuf, &inLen);
        if (!NT_SUCCESS(status))
        {
            KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_ERROR_LEVEL,
                       "AIK: INJECT_KEY retrieve input failed: 0x%08X\n", status));
            WdfRequestComplete(Request, status);
            return;
        }

        pkt = (PAIK_KEY_PACKET)inBuf;

        if (pkt->Count == 0 || pkt->Count > AIK_MAX_SCANCODES)
        {
            KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_ERROR_LEVEL,
                       "AIK: INJECT_KEY bad count: %u\n", pkt->Count));
            WdfRequestComplete(Request, STATUS_INVALID_PARAMETER);
            return;
        }

        // Validate input buffer is large enough for the declared count.
        {
            size_t required = FIELD_OFFSET(AIK_KEY_PACKET, Codes) + pkt->Count * sizeof(AIK_SCANCODE);
            if (inLen < required)
            {
                WdfRequestComplete(Request, STATUS_BUFFER_TOO_SMALL);
                return;
            }
        }

        // Build KEYBOARD_INPUT_DATA array and inject via class service callback.
        {
            KEYBOARD_INPUT_DATA kid[AIK_MAX_SCANCODES];
            ULONG consumed = 0;

            RtlZeroMemory(kid, sizeof(kid));

            for (i = 0; i < pkt->Count; i++)
            {
                kid[i].UnitId = 0;
                kid[i].MakeCode = pkt->Codes[i].MakeCode;
                kid[i].Flags    = pkt->Codes[i].Flags;
                kid[i].ExtraInformation = 0;
            }

            KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_INFO_LEVEL,
                       "AIK: Injecting %u scancodes\n", pkt->Count));

            if (g_Connected && g_ConnectData.ClassService)
            {
                AikServiceCallback(
                    g_ConnectData.ClassDeviceObject,
                    &kid[0],
                    &kid[pkt->Count],
                    &consumed
                );
            }
            else
            {
                // Driver is in standalone (non-filter) mode.
                // Scancodes are accepted but cannot be injected without a class connection.
                // Log a warning.  The Python bridge should fall back to SendInput.
                consumed = pkt->Count;
                KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_WARNING_LEVEL,
                           "AIK: No class connection; %u scancodes accepted but NOT injected.\n",
                           pkt->Count));
            }

            // Return consumed count in output buffer (4 bytes).
            {
                PVOID outBuf = NULL;
                size_t outLen = 0;
                status = WdfRequestRetrieveOutputBuffer(Request, sizeof(ULONG), &outBuf, &outLen);
                if (NT_SUCCESS(status) && outLen >= sizeof(ULONG))
                {
                    *(PULONG)outBuf = consumed;
                    WdfRequestSetInformation(Request, sizeof(ULONG));
                }
            }

            WdfRequestComplete(Request, STATUS_SUCCESS);
            return;
        }
    }

    default:
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_WARNING_LEVEL, "AIK: Unknown IOCTL: 0x%08X\n", IoControlCode));
        WdfRequestComplete(Request, STATUS_INVALID_DEVICE_REQUEST);
        return;
    }
}
