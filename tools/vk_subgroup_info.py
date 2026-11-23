"""Ask the Vulkan driver whether this PC's GPU can run 64-wide compute and mesh subgroups.

    python -I tools/vk_subgroup_info.py          # prints one JSON line, exit 0 if a device answered, 1 if not
    python -I tools/vk_subgroup_info.py --stage-caps  # includes the stage masks and workgroup limit

make_game.py uses it to choose between native wave64 translation and --wave64-emulate. It talks to the system's
Vulkan implementation through ctypes (standard library only), so it works before the runtime stage has built anything.
On macOS it opens MoltenVK directly, using the same LEONIDA_VULKAN_LOADER override as the C++ test tools.

Device choice follows the runtime (runtime/shims/d3d12_vulkan_device.cpp, runtime/src/vulkan_device_filter.h):
LEONIDA_VK_DEVICE filters by deviceName substring case-insensitively (failing when nothing matches). Eligible devices
need Vulkan 1.3 or Vulkan 1.2 with VK_KHR_dynamic_rendering and VK_KHR_synchronization2. They also need the corresponding
feature bits, timelineSemaphore, separateDepthStencilLayouts, bufferDeviceAddress, and a graphics queue family. Among
those the first of the highest rank wins: a native driver over Microsoft's Dozen
(vendor 0x1414, which reports a discrete GPU, #915), then discrete > integrated > virtual > CPU (vulkan_device_rank, as
vk_device_pick.h picks and, with #1799, the runtime). The runtime also needs the rest of its device setup (surface,
extensions); that is not checked here. vk_pipeline_check (the pipelines stage) picks through vk_device_pick.h.
VK_LOADER_DRIVERS_SELECT (e.g. '*amd*')
narrows which drivers the loader offers.

native64 matches the current translated SPIR-V 1.5 modules, which use a fixed subgroup size: compute and mesh must
support subgroup operations and either have a default subgroupSize of 64 or allow 64 to be required. native64_compute
applies the same check to compute alone, including mesh shaders translated with --mesh-compute. SPIR-V 1.6 modules
or stages created with ALLOW_VARYING_SUBGROUP_SIZE need a different check.

rdna1_amd_windows and rdna2_amd_windows identify those GPU families by PCI device ID under AMD's proprietary Windows
driver. Its shader compiler can crash in vkCreateComputePipelines on emulated wave64 compute shaders with LDS
(#857, reported on RX 5000, RX 6000 and RDNA2 iGPUs). make_game.py prefers native wave64 when the GPU can run it.

per_vertex_inputs says whether the device can run a pixel shader with PerVertexKHR inputs (16-bit interpolation,
docs/design.md): the fragmentShaderBarycentric feature, and a driver other than MoltenVK, whose SPIR-V to MSL
conversion rejects the decoration ("PerVertexKHR decoration is not supported in MSL", MoltenVK 1.4.2). Where it is
false, the shaders are translated with --f16-interp-fallback.

max_push_constants_size is VkPhysicalDeviceProperties.limits.maxPushConstantsSize in bytes. The default shader
contract uses 144 bytes (152 with --mesh-compute); where a device reports less (e.g. 128 on AMD's Windows driver for
RDNA2 iGPUs and some Intel Arc drivers), make_game.py translates with --pc-tail-ubo.
"""
import argparse
import ctypes
import json
import os
import sys

VK_STRUCTURE_TYPE_APPLICATION_INFO = 0
VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO = 1
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2 = 1000059000
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2 = 1000059001
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_PROPERTIES = 1000094000
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES = 51
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES = 53
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DYNAMIC_RENDERING_FEATURES_KHR = 1000044003
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES_KHR = 1000314007
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_SIZE_CONTROL_PROPERTIES = 1000225000
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES = 1000196000
VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FRAGMENT_SHADER_BARYCENTRIC_FEATURES_KHR = 1000203000
VK_DRIVER_ID_MOLTENVK = 14
VK_DRIVER_ID_AMD_PROPRIETARY = 1
AMD_VENDOR_ID = 0x1002
# Navi 10, Navi 14, and Navi 12 PCI IDs from Linux amdgpu's chipset table.
RDNA1_DEVICE_IDS = (0x7310, 0x7312, 0x7318, 0x7319, 0x731A, 0x731B, 0x731E, 0x731F,
                    0x7340, 0x7341, 0x7347, 0x734F, 0x7360, 0x7362)
# PCI device IDs of RDNA2 (gfx103x) GPUs: Navi 21/22/23/24 cards, and the iGPUs of Van Gogh (Steam Deck),
# Rembrandt (Ryzen 6000), Mendocino (Ryzen 7020) and Raphael/Granite Ridge (Ryzen 7000/9000 desktop). #857 has
# crash reports from 0x13C0, 0x164E, 0x73BF (RX 6800) and 0x73DF (RX 6700).
RDNA2_DEVICE_IDS = ((0x73A0, 0x73FF), (0x7420, 0x743F), (0x163F, 0x163F), (0x1435, 0x1435), (0x1681, 0x1681),
                    (0x1506, 0x1506), (0x164E, 0x164E), (0x13C0, 0x13C0))
DRIVER_PROPS_SIZE = 536     # sizeof(VkPhysicalDeviceDriverProperties): 16 + driverID + 2 x char[256] + version, padded
BARY_FEATURES_SIZE = 24     # sizeof(VkPhysicalDeviceFragmentShaderBarycentricFeaturesKHR): 16 + one VkBool32
VK_SHADER_STAGE_COMPUTE_BIT = 0x20
VK_SHADER_STAGE_MESH_BIT_EXT = 0x80
VK_QUEUE_GRAPHICS_BIT = 1
VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU = 2
VK_API_VERSION_1_2 = (1 << 22) | (2 << 12)
VK_API_VERSION_1_3 = (1 << 22) | (3 << 12)
VK_INCOMPLETE = 5
VULKAN_12_REQUIRED_EXTENSIONS = frozenset((b"VK_KHR_dynamic_rendering", b"VK_KHR_synchronization2"))

PROPS_SIZE = 824            # sizeof(VkPhysicalDeviceProperties) on 64-bit
FEATURES2_SIZE = 240        # sizeof(VkPhysicalDeviceFeatures2): 16 + 55 VkBool32, padded to 8
F12_SIZE = 208              # sizeof(VkPhysicalDeviceVulkan12Features): 16 + 47 VkBool32, padded to 8
F13_SIZE = 80               # sizeof(VkPhysicalDeviceVulkan13Features): 16 + 15 VkBool32, padded to 8
# Byte offsets of the VkBool32 members read below (16-byte header, then 4 bytes per member in header order)
F12_SEPARATE_DEPTH_STENCIL_LAYOUTS = 16 + 4 * 35
F12_TIMELINE_SEMAPHORE = 16 + 4 * 37
F12_BUFFER_DEVICE_ADDRESS = 16 + 4 * 38
F13_SUBGROUP_SIZE_CONTROL = 16 + 4 * 7
F13_SYNCHRONIZATION2 = 16 + 4 * 9
F13_DYNAMIC_RENDERING = 16 + 4 * 12
PROPS_DEVICE_TYPE = 16      # offsets in VkPhysicalDeviceProperties
PROPS_VENDOR_ID = 8
PROPS_DEVICE_ID = 12
PROPS_DEVICE_NAME = 20      # char[256]
PROPS_API_VERSION = 0
PROPS_MAX_PUSH_CONSTANTS_SIZE = 296 + 32  # limits (296) + maxPushConstantsSize (32) on 64-bit
QUEUE_FAMILY_SIZE = 24      # sizeof(VkQueueFamilyProperties)


class AppInfo(ctypes.Structure):
    _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("pApplicationName", ctypes.c_char_p),
                ("applicationVersion", ctypes.c_uint32), ("pEngineName", ctypes.c_char_p),
                ("engineVersion", ctypes.c_uint32), ("apiVersion", ctypes.c_uint32)]


class InstanceInfo(ctypes.Structure):
    _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("flags", ctypes.c_uint32),
                ("pApplicationInfo", ctypes.POINTER(AppInfo)), ("enabledLayerCount", ctypes.c_uint32),
                ("ppEnabledLayerNames", ctypes.c_void_p), ("enabledExtensionCount", ctypes.c_uint32),
                ("ppEnabledExtensionNames", ctypes.c_void_p)]


class SubgroupSizeControl(ctypes.Structure):
    _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("minSubgroupSize", ctypes.c_uint32),
                ("maxSubgroupSize", ctypes.c_uint32), ("maxComputeWorkgroupSubgroups", ctypes.c_uint32),
                ("requiredSubgroupSizeStages", ctypes.c_uint32)]


class SubgroupProperties(ctypes.Structure):
    _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("subgroupSize", ctypes.c_uint32),
                ("supportedStages", ctypes.c_uint32), ("supportedOperations", ctypes.c_uint32),
                ("quadOperationsInAllStages", ctypes.c_uint32)]


class ExtensionProperties(ctypes.Structure):
    _fields_ = [("extensionName", ctypes.c_char * 256), ("specVersion", ctypes.c_uint32)]


def block(size: int, stype: int):
    """A zeroed struct of `size` bytes with sType set (sType and pNext head every Vulkan struct used here)."""
    buf = ctypes.create_string_buffer(size)
    ctypes.c_uint32.from_buffer(buf, 0).value = stype
    return buf


def set_next(buf, other) -> None:
    ctypes.c_void_p.from_buffer(buf, 8).value = ctypes.addressof(other)


def u32(buf, off: int) -> int:
    return ctypes.c_uint32.from_buffer(buf, off).value


def load_vulkan():
    if os.name == "nt":
        names = ["vulkan-1.dll"]
    elif sys.platform == "darwin":
        override = os.environ.get("LEONIDA_VULKAN_LOADER")
        names = [override] if override else ["libMoltenVK.dylib", "/opt/homebrew/lib/libMoltenVK.dylib"]
    else:
        names = ["libvulkan.so.1", "libvulkan.1.dylib"]
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError:
            pass
    raise RuntimeError("no Vulkan loader found (" + ", ".join(names) + ")")


def rdna2_amd_windows(vendor_id: int, device_id: int, driver_id: int) -> bool:
    """An RDNA2 GPU under AMD's proprietary (Windows) driver: the one whose compiler crashes on #857's modules."""
    return (vendor_id == AMD_VENDOR_ID and driver_id == VK_DRIVER_ID_AMD_PROPRIETARY
            and any(lo <= device_id <= hi for lo, hi in RDNA2_DEVICE_IDS))


def physical_devices(vk, instance):
    # The list can grow between the count and fetch. Recount, with a bound for a changing driver.
    for _ in range(3):
        count = ctypes.c_uint32()
        result = vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None)
        if result != 0:
            raise RuntimeError(f"vkEnumeratePhysicalDevices count failed ({result})")
        if not count.value:
            return []
        devices = (ctypes.c_void_p * count.value)()
        result = vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), devices)
        if result == 0:
            return devices[:count.value]
        if result != VK_INCOMPLETE:
            raise RuntimeError(f"vkEnumeratePhysicalDevices failed ({result})")
    raise RuntimeError("vkEnumeratePhysicalDevices repeatedly returned VK_INCOMPLETE")


def device_extensions(vk: ctypes.CDLL, device: ctypes.c_void_p) -> set[bytes]:
    """Read one device's extension names, retrying if the list changed between calls."""
    for _ in range(3):
        count = ctypes.c_uint32()
        result = vk.vkEnumerateDeviceExtensionProperties(device, None, ctypes.byref(count), None)
        if result != 0 or not count.value:
            break
        props = (ExtensionProperties * count.value)()
        result = vk.vkEnumerateDeviceExtensionProperties(device, None, ctypes.byref(count), props)
        if result == 0:
            return {entry.extensionName for entry in props[:count.value]}
        if result != VK_INCOMPLETE:
            break
    return set()


MICROSOFT_VENDOR_ID = 0x1414   # Dozen and the Basic Render Driver; no native GPU driver uses it


def device_rank(device_type: int, vendor_id: int) -> int:
    """vulkan_device_rank() (runtime/src/vulkan_device_filter.h, #1799) and vk_device_pick.h: a native driver over
    Microsoft's layers (Dozen reports a discrete GPU, #915), then discrete > integrated > virtual > CPU > other."""
    type_rank = {2: 4, 1: 3, 3: 2, 4: 1}.get(device_type, 0)   # DISCRETE, INTEGRATED, VIRTUAL, CPU
    return (0 if vendor_id == MICROSOFT_VENDOR_ID else 10) + type_rank


def query(*, stage_caps=False) -> dict:
    """The answer for the device the runtime would pick; raises RuntimeError with the reason when there is none."""
    if sys.maxsize <= 2**32:
        # the struct sizes below are the 64-bit ones, and 32-bit Windows Vulkan is stdcall, not CDLL's cdecl
        raise RuntimeError("needs a 64-bit Python")
    vk = load_vulkan()
    vp = ctypes.c_void_p
    vk.vkCreateInstance.argtypes = [vp, vp, vp]
    vk.vkEnumeratePhysicalDevices.argtypes = [vp, vp, vp]
    vk.vkEnumerateDeviceExtensionProperties.argtypes = [vp, vp, vp, vp]
    vk.vkGetPhysicalDeviceProperties2.argtypes = [vp, vp]
    vk.vkGetPhysicalDeviceFeatures2.argtypes = [vp, vp]
    vk.vkGetPhysicalDeviceQueueFamilyProperties.argtypes = [vp, vp, vp]
    vk.vkDestroyInstance.argtypes = [vp, vp]
    app = AppInfo(VK_STRUCTURE_TYPE_APPLICATION_INFO, None, b"leonida-subgroup-info", 1, None, 0, VK_API_VERSION_1_3)
    info = InstanceInfo(VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, None, 0, ctypes.pointer(app), 0, None, 0, None)
    inst = ctypes.c_void_p()
    r = vk.vkCreateInstance(ctypes.byref(info), None, ctypes.byref(inst))
    if r != 0:
        raise RuntimeError(f"vkCreateInstance failed ({r})")
    try:
        devs = physical_devices(vk, inst)
        if not devs:
            raise RuntimeError("no Vulkan device")
        device_filter = os.environ.get("LEONIDA_VK_DEVICE", "")
        wanted = device_filter.lower()
        chosen = None
        chosen_rank = -1
        for d in devs:
            d = ctypes.c_void_p(d)
            sg = SubgroupSizeControl(VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_SIZE_CONTROL_PROPERTIES)
            subgroup = SubgroupProperties()
            subgroup.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_PROPERTIES
            subgroup.pNext = ctypes.addressof(sg)
            drv = block(DRIVER_PROPS_SIZE, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES)
            sg.pNext = ctypes.addressof(drv)
            props = block(16 + PROPS_SIZE, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2)
            set_next(props, subgroup)
            vk.vkGetPhysicalDeviceProperties2(d, props)
            raw = bytes(props)[16:]
            dev_name = raw[PROPS_DEVICE_NAME:PROPS_DEVICE_NAME + 256].split(b"\0")[0].decode("utf-8", "replace")
            version = int.from_bytes(raw[PROPS_API_VERSION:PROPS_API_VERSION + 4], "little")
            if version < VK_API_VERSION_1_2 or (wanted and wanted not in dev_name.lower()):
                continue
            core13 = version >= VK_API_VERSION_1_3
            if not core13 and not VULKAN_12_REQUIRED_EXTENSIONS.issubset(device_extensions(vk, d)):
                continue
            f13 = block(F13_SIZE, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES)
            f12 = block(F12_SIZE, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES)
            bary = block(BARY_FEATURES_SIZE, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FRAGMENT_SHADER_BARYCENTRIC_FEATURES_KHR)
            if core13:
                set_next(f12, f13)
                set_next(f13, bary)
            else:
                rendering = block(24, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DYNAMIC_RENDERING_FEATURES_KHR)
                sync2 = block(24, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES_KHR)
                set_next(f12, rendering)
                set_next(rendering, sync2)
                set_next(sync2, bary)
            f2 = block(FEATURES2_SIZE, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2)
            set_next(f2, f12)
            vk.vkGetPhysicalDeviceFeatures2(d, f2)
            dynamic_rendering = u32(f13, F13_DYNAMIC_RENDERING) if core13 else u32(rendering, 16)
            synchronization2 = u32(f13, F13_SYNCHRONIZATION2) if core13 else u32(sync2, 16)
            if not (dynamic_rendering and synchronization2
                    and u32(f12, F12_TIMELINE_SEMAPHORE) and u32(f12, F12_SEPARATE_DEPTH_STENCIL_LAYOUTS)
                    and u32(f12, F12_BUFFER_DEVICE_ADDRESS)):
                continue
            fam = ctypes.c_uint32(0)
            vk.vkGetPhysicalDeviceQueueFamilyProperties(d, ctypes.byref(fam), None)
            fams = ctypes.create_string_buffer(QUEUE_FAMILY_SIZE * max(fam.value, 1))
            vk.vkGetPhysicalDeviceQueueFamilyProperties(d, ctypes.byref(fam), fams)
            if not any(u32(fams, QUEUE_FAMILY_SIZE * i) & VK_QUEUE_GRAPHICS_BIT for i in range(fam.value)):
                continue
            dtype = int.from_bytes(raw[PROPS_DEVICE_TYPE:PROPS_DEVICE_TYPE + 4], "little")
            vendor_id = int.from_bytes(raw[PROPS_VENDOR_ID:PROPS_VENDOR_ID + 4], "little")
            # the runtime's and the harnesses' rule: the first usable device of the highest rank
            rank = device_rank(dtype, vendor_id)
            if rank > chosen_rank:
                chosen_rank = rank
                chosen = (dev_name,
                          subgroup.subgroupSize, subgroup.supportedStages, sg.minSubgroupSize, sg.maxSubgroupSize,
                          sg.requiredSubgroupSizeStages, sg.maxComputeWorkgroupSubgroups,
                          bool(u32(f13, F13_SUBGROUP_SIZE_CONTROL)),
                          bool(u32(bary, 16)) and u32(drv, 16) != VK_DRIVER_ID_MOLTENVK,
                          int.from_bytes(raw[PROPS_MAX_PUSH_CONSTANTS_SIZE:PROPS_MAX_PUSH_CONSTANTS_SIZE + 4], "little"),
                          int.from_bytes(raw[PROPS_VENDOR_ID:PROPS_VENDOR_ID + 4], "little"),
                          int.from_bytes(raw[PROPS_DEVICE_ID:PROPS_DEVICE_ID + 4], "little"), u32(drv, 16))
        if chosen is None:
            if device_filter:
                raise RuntimeError(
                    f"LEONIDA_VK_DEVICE={device_filter}: no Vulkan 1.3 GPU with the features and graphics queue "
                    "the runtime needs matches"
                )
            raise RuntimeError("no Vulkan 1.3 GPU with the features and graphics queue the runtime needs")
        (name, subgroup_size, supported_stages, lo, hi, required_stages, max_workgroup_subgroups,
         sgc, per_vertex, max_push_constants_size, vendor, device, driver) = chosen
        compute_required = bool(required_stages & VK_SHADER_STAGE_COMPUTE_BIT)
        mesh_required = bool(required_stages & VK_SHADER_STAGE_MESH_BIT_EXT)
        compute_supported = bool(supported_stages & VK_SHADER_STAGE_COMPUTE_BIT)
        mesh_supported = bool(supported_stages & VK_SHADER_STAGE_MESH_BIT_EXT)
        can_require_64 = sgc and lo <= 64 <= hi
        default_64 = subgroup_size == 64
        compute64 = compute_supported and (default_64 or (can_require_64 and compute_required))
        mesh64 = mesh_supported and (default_64 or (can_require_64 and mesh_required))
        result = {
            "device": name,
            "subgroup_size": subgroup_size,
            "min_subgroup_size": lo,
            "max_subgroup_size": hi,
            "subgroup_size_control": sgc,
            "compute_in_required_stages": compute_required,
            "mesh_in_required_stages": mesh_required,
            "compute_in_supported_stages": compute_supported,
            "mesh_in_supported_stages": mesh_supported,
            "native64_compute": compute64,
            "native64": compute64 and mesh64,
            "per_vertex_inputs": per_vertex,
            "max_push_constants_size": max_push_constants_size,
            "vendor_id": vendor,
            "device_id": device,
            "driver_id": driver,
            "rdna1_amd_windows": (vendor == AMD_VENDOR_ID and driver == VK_DRIVER_ID_AMD_PROPRIETARY
                                  and device in RDNA1_DEVICE_IDS),
            "rdna2_amd_windows": rdna2_amd_windows(vendor, device, driver),
        }
        if stage_caps:
            result.update(supported_subgroup_stages=supported_stages,
                          required_subgroup_size_stages=required_stages,
                          max_compute_workgroup_subgroups=max_workgroup_subgroups)
        return result
    finally:
        vk.vkDestroyInstance(inst, None)


def main(*, stage_caps=False) -> int:
    try:
        print(json.dumps(query(stage_caps=stage_caps)))
        return 0
    except Exception as e:      # any failure means "unknown": the caller falls back to emulation
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1


if __name__ == "__main__":
    try:  # a redirected stdout is strict ANSI code page under -I; write '?' for what it cannot encode
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage-caps", action="store_true", help="include raw subgroup stage masks and limits")
    sys.exit(main(stage_caps=parser.parse_args().stage_caps))
