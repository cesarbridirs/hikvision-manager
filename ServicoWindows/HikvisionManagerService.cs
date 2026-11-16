using System;
using System.Configuration;
using System.Diagnostics;
using System.IO;
using System.ServiceProcess;
using System.Threading;

namespace HikvisionManagerService
{
    public class Program
    {
        public static void Main(string[] args)
        {
            if (args.Length > 0 && args[0].Equals("--console", StringComparison.OrdinalIgnoreCase))
            {
                var runner = new ServerRunner();
                runner.Start();
                Console.WriteLine("Servidor iniciado. Pressione ENTER para parar.");
                Console.ReadLine();
                runner.Stop();
                return;
            }

            ServiceBase.Run(new WindowsService());
        }
    }

    public class WindowsService : ServiceBase
    {
        private readonly ServerRunner runner = new ServerRunner();

        public WindowsService()
        {
            ServiceName = "HikvisionManagerApp";
            CanStop = true;
            CanShutdown = true;
            AutoLog = true;
        }

        protected override void OnStart(string[] args)
        {
            runner.Start();
        }

        protected override void OnStop()
        {
            runner.Stop();
        }

        protected override void OnShutdown()
        {
            runner.Stop();
            base.OnShutdown();
        }
    }

    public class ServerRunner
    {
        private Thread supervisor;
        private readonly ManualResetEvent stopped = new ManualResetEvent(false);
        private readonly object logLock = new object();
        private StreamWriter logWriter;

        public void Start()
        {
            if (supervisor != null && supervisor.IsAlive) return;
            string root = ReadSetting("RootPath", AppDomain.CurrentDomain.BaseDirectory);
            string logPath = ReadSetting("LogPath", Path.Combine(root, "logs", "hikvision-manager-service.log"));
            Directory.CreateDirectory(Path.GetDirectoryName(logPath));
            logWriter = new StreamWriter(new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite));
            logWriter.AutoFlush = true;
            stopped.Reset();
            supervisor = new Thread(Supervise);
            supervisor.IsBackground = true;
            supervisor.Start();
        }

        private void Supervise()
        {
            int delaySeconds = 5;
            while (!stopped.WaitOne(0))
            {
                DateTime startedAt = DateTime.UtcNow;
                try
                {
                    string root = ReadSetting("RootPath", AppDomain.CurrentDomain.BaseDirectory);
                    using (var process = new Process())
                    {
                        process.StartInfo = new ProcessStartInfo
                        {
                            FileName = ReadSetting("PythonPath", "python.exe"),
                            Arguments = "-u \"" + ReadSetting("AppPath", Path.Combine(root, "backend", "app.py")) + "\"",
                            WorkingDirectory = root,
                            UseShellExecute = false,
                            RedirectStandardOutput = true,
                            RedirectStandardError = true,
                            CreateNoWindow = true
                        };
                        process.OutputDataReceived += (sender, args) => { if (args.Data != null) Log(args.Data); };
                        process.ErrorDataReceived += (sender, args) => { if (args.Data != null) Log("ERRO: " + args.Data); };
                        Log("Iniciando servidor");
                        process.Start();
                        process.BeginOutputReadLine();
                        process.BeginErrorReadLine();
                        while (!process.WaitForExit(500))
                        {
                            if (stopped.WaitOne(0))
                            {
                                try { process.Kill(); }
                                catch (InvalidOperationException) { }
                                break;
                            }
                        }
                        process.WaitForExit();
                        Log("Servidor finalizado com codigo " + process.ExitCode);
                    }
                }
                catch (Exception error)
                {
                    Log("Falha no processo: " + error.Message);
                }
                if (stopped.WaitOne(0)) break;
                if ((DateTime.UtcNow - startedAt).TotalMinutes >= 5) delaySeconds = 5;
                Log("Reiniciando em " + delaySeconds + " segundos");
                if (stopped.WaitOne(delaySeconds * 1000)) break;
                delaySeconds = Math.Min(60, delaySeconds * 2);
            }
        }

        public void Stop()
        {
            stopped.Set();
            if (supervisor != null && !supervisor.Join(15000))
                throw new System.TimeoutException("O processo do servidor nao encerrou dentro de 15 segundos.");
            supervisor = null;
            lock (logLock)
            {
                Log("Servico parado");
                if (logWriter != null) logWriter.Dispose();
                logWriter = null;
            }
        }

        private static string ReadSetting(string key, string fallback)
        {
            string value = ConfigurationManager.AppSettings[key];
            return string.IsNullOrWhiteSpace(value) ? fallback : value;
        }

        private void Log(string message)
        {
            lock (logLock)
            {
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message;
                try
                {
                    if (logWriter != null) logWriter.WriteLine(line);
                    else Console.WriteLine(line);
                }
                catch (IOException) { }
            }
        }
    }
}
