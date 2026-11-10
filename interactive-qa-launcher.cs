// Local QA entry point. SOURCE_MODE runs this checkout; the packaged build runs
// its sibling executable. No browser, shell commands or input automation.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle(__APP_QA_TITLE_LITERAL__)]
[assembly: AssemblyDescription(__APP_QA_TITLE_LITERAL__)]
[assembly: AssemblyProduct(__APP_NAME_LITERAL__)]
[assembly: AssemblyVersion(__APP_VERSION_LITERAL__)]
[assembly: AssemblyFileVersion(__APP_VERSION_LITERAL__)]

internal static class GuguguQaLauncher
{
    private static string QuoteArgument(string value)
    {
        StringBuilder text = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                slashes++;
                continue;
            }
            if (character == '"')
            {
                text.Append('\\', slashes * 2 + 1);
                text.Append('"');
            }
            else
            {
                text.Append('\\', slashes);
                text.Append(character);
            }
            slashes = 0;
        }
        text.Append('\\', slashes * 2);
        return text.Append('"').ToString();
    }

    [STAThread]
    private static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        try
        {
#if SOURCE_MODE
            string executable = Path.GetFullPath(Path.Combine(root, ".venv", "Scripts", "pythonw.exe"));
            string script = Path.Combine(root, "app.py");
            string profile = Path.Combine(root, "qa-profile-source");
            if (!File.Exists(script)) throw new FileNotFoundException("The source app is not ready.", script);
            string arguments = QuoteArgument(script) + " --self-test --data-dir " + QuoteArgument(profile);
#else
            string executable = Path.Combine(root, __APP_BINARY_LITERAL__);
            string profile = Path.Combine(root, "qa-profile-packaged");
            string arguments = "--self-test --data-dir " + QuoteArgument(profile);
#endif
            if (!File.Exists(executable)) throw new FileNotFoundException("The application executable is unavailable.", executable);
            ProcessStartInfo start = new ProcessStartInfo();
            start.FileName = executable;
            start.Arguments = arguments;
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            using (Process child = Process.Start(start))
            {
                if (child == null) throw new InvalidOperationException("Process.Start did not return a process.");
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            try { File.WriteAllText(Path.Combine(root, "qa-launch-error.txt"), error.ToString(), new UTF8Encoding(false)); }
            catch { }
            return 1;
        }
    }
}
