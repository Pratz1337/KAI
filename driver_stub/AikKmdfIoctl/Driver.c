#include <ntddk.h>
#include <wdf.h>

#include "Public.h"

DRIVER_INITIALIZE DriverEntry;
EVT_WDF_DRIVER_DEVICE_ADD AikEvtDeviceAdd;
EVT_WDF_OBJECT_CONTEXT_CLEANUP AikEvtDriverContextCleanup;

NTSTATUS AikCreateDevice(_Inout_ PWDFDEVICE_INIT DeviceInit);

VOID AikEvtDriverContextCleanup(_In_ WDFOBJECT DriverObject)
{
    UNREFERENCED_PARAMETER(DriverObject);
    KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_INFO_LEVEL, "AIK: Driver cleanup\n"));
}

NTSTATUS DriverEntry(_In_ PDRIVER_OBJECT DriverObject, _In_ PUNICODE_STRING RegistryPath)
{
    WDF_DRIVER_CONFIG config;
    NTSTATUS status;
    WDF_OBJECT_ATTRIBUTES attrs;

    KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_INFO_LEVEL, "AIK: DriverEntry\n"));

    WDF_DRIVER_CONFIG_INIT(&config, AikEvtDeviceAdd);

    WDF_OBJECT_ATTRIBUTES_INIT(&attrs);
    attrs.EvtCleanupCallback = AikEvtDriverContextCleanup;

    status = WdfDriverCreate(DriverObject, RegistryPath, &attrs, &config, WDF_NO_HANDLE);
    if (!NT_SUCCESS(status))
    {
        KdPrintEx((DPFLTR_IHVDRIVER_ID, DPFLTR_ERROR_LEVEL, "AIK: WdfDriverCreate failed: 0x%08X\n", status));
        return status;
    }

    return STATUS_SUCCESS;
}

NTSTATUS AikEvtDeviceAdd(_In_ WDFDRIVER Driver, _Inout_ PWDFDEVICE_INIT DeviceInit)
{
    UNREFERENCED_PARAMETER(Driver);
    return AikCreateDevice(DeviceInit);
}

