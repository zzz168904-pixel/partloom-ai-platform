using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swpublished;

namespace AICADAgent.SolidWorks
{
    [ComVisible(true)]
    [Guid(AddinGuid)]
    [ProgId("AICADAgent.SolidWorks.Addin")]
    public sealed class SolidWorksAddin : ISwAddin
    {
        public const string AddinGuid = "D2A1EBA5-75E3-4BB6-923C-7B92E944FC89";

        private ISldWorks _application;
        private ITaskpaneView _taskPane;
        private AgentTaskPaneControl _control;
        private GatewayProcessManager _gatewayProcess;

        public bool ConnectToSW(object thisSw, int cookie)
        {
            try
            {
                _application = (ISldWorks)thisSw;
                // SOLIDWORKS' official .NET control sample uses the legacy
                // callback registration for managed add-ins. On SW 2025 the
                // Info2 marshaler rejects a ClassInterface-less callback object.
                _application.SetAddinCallbackInfo(0, this, cookie);
                _gatewayProcess = new GatewayProcessManager();
                _gatewayProcess.EnsureRunning();
                _control = new AgentTaskPaneControl(_gatewayProcess);
                string iconPath = CreateTaskPaneIcon();
                _taskPane = (ITaskpaneView)_application.CreateTaskpaneView2(iconPath, "AI CAD Agent");
                if (_taskPane == null)
                {
                    throw new InvalidOperationException("SOLIDWORKS did not create the AI CAD Agent Task Pane.");
                }
                if (!_taskPane.DisplayWindowFromHandlex64(_control.Handle.ToInt64()))
                {
                    throw new InvalidOperationException("SOLIDWORKS could not host the AI CAD Agent control.");
                }
                _taskPane.ShowView();
                return true;
            }
            catch (Exception exception)
            {
                System.Windows.Forms.MessageBox.Show(
                    exception.ToString(),
                    "AI CAD Agent Add-in",
                    System.Windows.Forms.MessageBoxButtons.OK,
                    System.Windows.Forms.MessageBoxIcon.Error);
                return false;
            }
        }

        public bool DisconnectFromSW()
        {
            try
            {
                if (_taskPane != null)
                {
                    _taskPane.DeleteView();
                }
                if (_control != null)
                {
                    _control.Dispose();
                }
            }
            finally
            {
                _taskPane = null;
                _control = null;
                _application = null;
                _gatewayProcess = null;
            }
            return true;
        }

        private static string CreateTaskPaneIcon()
        {
            string root = Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.LocalApplicationData), "SolidWorksAIAgent");
            Directory.CreateDirectory(root);
            string path = Path.Combine(root, "AI_CAD_Agent_TaskPane_Icon.bmp");
            if (File.Exists(path))
            {
                return path;
            }
            using (Bitmap bitmap = new Bitmap(20, 20))
            using (Graphics graphics = Graphics.FromImage(bitmap))
            using (Brush background = new SolidBrush(Color.FromArgb(27, 30, 32)))
            using (Brush accent = new SolidBrush(Color.FromArgb(0, 194, 168)))
            using (Font font = new Font("Arial", 7.0F, FontStyle.Bold, GraphicsUnit.Pixel))
            {
                graphics.FillRectangle(background, 0, 0, 20, 20);
                graphics.FillRectangle(accent, 2, 2, 16, 16);
                graphics.DrawString("AI", font, Brushes.Black, 4, 6);
                bitmap.Save(path, ImageFormat.Bmp);
            }
            return path;
        }
    }
}
