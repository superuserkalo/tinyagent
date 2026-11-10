using Microsoft.Win32.SafeHandles;
using System.IO.Pipes;

namespace TinyAgent;

internal static class Stdio
{
    internal static Stream Open(int descriptor, FileAccess access)
    {
        // Query seekability without taking ownership or starting an I/O operation.
        using var borrowed = new FileStream(new SafeFileHandle(descriptor, false), access, 1, false);
        if (borrowed.CanSeek)
            return new FileStream(new SafeFileHandle(descriptor, true), access, 4096, false);

        // Ownership is essential: cancellation followed by disposal must release native pipe I/O.
        return new AnonymousPipeClientStream(access == FileAccess.Read ? PipeDirection.In : PipeDirection.Out,
            new SafePipeHandle(descriptor, true));
    }
}
