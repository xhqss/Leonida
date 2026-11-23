// Static TLS reserve for the loader's own executable.
//
// With LEONIDA_GUEST_TLS_SLOT0=1 the loader moves this executable's own static TLS to a helper module's index and
// gives module TLS index 0 to the mapped image, whose code reads that index with a literal 0 (the comment on
// Loader::relocate_host_tls in pe_loader.cpp has the measurements). Two sizes then have to line up:
//
//  - The image's block is installed at index 0, and this executable's own block is what a thread that the loader could
//    not reach (it was starting or ending while the image was mapped) still reads through the index it thinks it has.
//    The reserve below makes this executable's block larger than the retail image's TLS template (5248 bytes,
//    measured), so index 0 is big enough for the image even on such a thread.
//  - Loader::initialize_tls refuses index 0 for an image whose TLS block is larger than this one, so the check, not
//    this number, decides; the reserve exists to make the answer yes for the image this runtime runs.
//
// The first byte is set so the object stays in the image's raw TLS data instead of being folded into zero fill.
__declspec(thread) unsigned char g_host_tls_reserve[8192] = {1};
