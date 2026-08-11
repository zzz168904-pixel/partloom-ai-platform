using System;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;

namespace AICADAgent.SolidWorks
{
    public sealed class GatewayProcessManager
    {
        public const int Port = 8765;

        private readonly string _projectRoot;
        private readonly string _token;
        private Process _process;

        public GatewayProcessManager()
        {
            _projectRoot = FindProjectRoot();
            _token = LoadOrCreateToken();
        }

        public string ProjectRoot { get { return _projectRoot; } }
        public string Token { get { return _token; } }
        public string BaseUrl { get { return "http://127.0.0.1:" + Port.ToString(); } }

        public void EnsureRunning()
        {
            if (IsPortOpen())
            {
                return;
            }
            string python = Path.Combine(_projectRoot, ".venv", "Scripts", "python.exe");
            if (!File.Exists(python))
            {
                throw new FileNotFoundException("Project Python runtime is missing. Run scripts\\Setup-AICADAgent.ps1.", python);
            }
            string outputRoot = Path.Combine(_projectRoot, "logs", "gateway_tasks");
            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = python;
            startInfo.WorkingDirectory = _projectRoot;
            startInfo.Arguments = string.Format(
                "-m cad_agent.gateway.server --port {0} --token \"{1}\" --output-root \"{2}\"",
                Port,
                _token,
                outputRoot);
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.WindowStyle = ProcessWindowStyle.Hidden;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;
            startInfo.EnvironmentVariables["PYTHONPATH"] = Path.Combine(_projectRoot, "src");
            startInfo.EnvironmentVariables["CAD_AGENT_NO_INTERACTIVE"] = "1";
            string logRoot = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SolidWorksAIAgent");
            Directory.CreateDirectory(logRoot);
            string logPath = Path.Combine(logRoot, "gateway-process.log");
            _process = new Process();
            _process.StartInfo = startInfo;
            _process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args) { AppendProcessLog(logPath, args.Data); };
            _process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args) { AppendProcessLog(logPath, args.Data); };
            _process.Start();
            _process.BeginOutputReadLine();
            _process.BeginErrorReadLine();
            for (int attempt = 0; attempt < 60; attempt++)
            {
                if (IsPortOpen())
                {
                    return;
                }
                if (_process != null && _process.HasExited)
                {
                    throw new InvalidOperationException("AI CAD Agent Gateway exited during startup. See " + logPath);
                }
                Thread.Sleep(250);
            }
            throw new TimeoutException("AI CAD Agent Gateway did not become ready on localhost:8765.");
        }

        private static void AppendProcessLog(string path, string value)
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                return;
            }
            try
            {
                lock (typeof(GatewayProcessManager))
                {
                    File.AppendAllText(path, DateTime.Now.ToString("s") + " " + value + Environment.NewLine, Encoding.UTF8);
                }
            }
            catch
            {
            }
        }

        private bool IsPortOpen()
        {
            try
            {
                using (TcpClient client = new TcpClient())
                {
                    IAsyncResult result = client.BeginConnect("127.0.0.1", Port, null, null);
                    bool connected = result.AsyncWaitHandle.WaitOne(150);
                    if (!connected)
                    {
                        return false;
                    }
                    client.EndConnect(result);
                    return true;
                }
            }
            catch
            {
                return false;
            }
        }

        private static string FindProjectRoot()
        {
            string configured = Environment.GetEnvironmentVariable("CAD_AGENT_PROJECT_ROOT");
            if (!string.IsNullOrWhiteSpace(configured) && Directory.Exists(Path.Combine(configured, "src", "cad_agent")))
            {
                return Path.GetFullPath(configured);
            }
            DirectoryInfo cursor = new DirectoryInfo(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));
            while (cursor != null)
            {
                if (Directory.Exists(Path.Combine(cursor.FullName, "src", "cad_agent")) && File.Exists(Path.Combine(cursor.FullName, "app.py")))
                {
                    return cursor.FullName;
                }
                cursor = cursor.Parent;
            }
            throw new DirectoryNotFoundException("Set CAD_AGENT_PROJECT_ROOT to the solidworks-ai-assistant project directory.");
        }

        private static string LoadOrCreateToken()
        {
            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SolidWorksAIAgent");
            Directory.CreateDirectory(root);
            string path = Path.Combine(root, "gateway.token");
            if (File.Exists(path))
            {
                string existing = File.ReadAllText(path, Encoding.ASCII).Trim();
                if (existing.Length > 20)
                {
                    return existing;
                }
            }
            string token = Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N");
            File.WriteAllText(path, token, Encoding.ASCII);
            return token;
        }
    }
}
