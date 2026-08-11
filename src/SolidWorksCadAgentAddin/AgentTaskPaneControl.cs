using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace AICADAgent.SolidWorks
{
    public sealed class AgentTaskPaneControl : UserControl
    {
        private static readonly Color Background = Color.FromArgb(17, 19, 21);
        private static readonly Color Surface = Color.FromArgb(27, 30, 32);
        private static readonly Color SurfaceRaised = Color.FromArgb(35, 39, 42);
        private static readonly Color Border = Color.FromArgb(55, 62, 66);
        private static readonly Color TextPrimary = Color.FromArgb(242, 246, 247);
        private static readonly Color TextSecondary = Color.FromArgb(164, 174, 179);
        private static readonly Color Accent = Color.FromArgb(0, 194, 168);
        private static readonly Color Warning = Color.FromArgb(242, 184, 75);
        private static readonly Color Danger = Color.FromArgb(240, 100, 100);

        private readonly GatewayProcessManager _gatewayProcess;
        private readonly GatewayClient _gateway;
        private readonly Timer _pollTimer;
        private readonly Timer _hostResizeTimer;
        private readonly TextBox _prompt;
        private readonly ComboBox _provider;
        private readonly ComboBox _taskMode;
        private readonly Label _connection;
        private readonly Label _taskStatus;
        private readonly Label _currentStep;
        private readonly RichTextBox _plan;
        private readonly RichTextBox _log;
        private readonly ProgressBar _progress;
        private readonly Button _planButton;
        private readonly Button _confirmButton;
        private readonly Button _cancelButton;
        private readonly Button _retryButton;
        private readonly Button _openOutputButton;
        private string _sourceType = "text";
        private string _sourcePath = string.Empty;
        private string _conversionMode = "2d";
        private string _taskId = string.Empty;
        private string _lastOutputPath = string.Empty;
        private int _eventCursor = -1;

        public AgentTaskPaneControl(GatewayProcessManager gatewayProcess)
        {
            _gatewayProcess = gatewayProcess;
            _gateway = new GatewayClient(gatewayProcess.BaseUrl, gatewayProcess.Token);
            Dock = DockStyle.Fill;
            Size = new Size(420, 760);
            MinimumSize = new Size(300, 420);
            BackColor = Background;
            ForeColor = TextPrimary;
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular, GraphicsUnit.Point);

            TableLayoutPanel root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.BackColor = Background;
            root.Padding = new Padding(12);
            root.ColumnCount = 1;
            root.RowCount = 8;
            root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 54F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 34F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 118F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 66F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 150F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 72F));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            root.RowStyles.Add(new RowStyle(SizeType.Absolute, 72F));
            Controls.Add(root);

            Panel header = new Panel();
            header.Dock = DockStyle.Fill;
            Label title = Label("AI CAD Agent", 15F, FontStyle.Bold, TextPrimary);
            title.Location = new Point(0, 2);
            title.AutoSize = true;
            Label subtitle = Label("SOLIDWORKS / AutoCAD / PDF2CAD", 8.5F, FontStyle.Regular, TextSecondary);
            subtitle.Location = new Point(1, 31);
            subtitle.AutoSize = true;
            _connection = Label("Connecting", 8.5F, FontStyle.Bold, Warning);
            _connection.AutoSize = true;
            _connection.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            _connection.Location = new Point(235, 8);
            header.Controls.Add(title);
            header.Controls.Add(subtitle);
            header.Controls.Add(_connection);
            root.Controls.Add(header, 0, 0);

            TableLayoutPanel selectors = new TableLayoutPanel();
            selectors.Dock = DockStyle.Fill;
            selectors.ColumnCount = 2;
            selectors.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            selectors.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
            _provider = Combo();
            _taskMode = Combo();
            _taskMode.Items.Add(new ComboItem("自动识别", "auto"));
            _taskMode.Items.Add(new ComboItem("仅3D建模", "model_3d"));
            _taskMode.Items.Add(new ComboItem("修改当前模型", "modify_3d"));
            _taskMode.Items.Add(new ComboItem("仅工程图", "create_drawing"));
            _taskMode.Items.Add(new ComboItem("仅自动标注", "annotate_drawing"));
            _taskMode.Items.Add(new ComboItem("仅导出文件", "export_files"));
            _taskMode.Items.Add(new ComboItem("完整流程", "full_pipeline"));
            _taskMode.SelectedIndex = 0;
            selectors.Controls.Add(_taskMode, 0, 0);
            selectors.Controls.Add(_provider, 1, 0);
            root.Controls.Add(selectors, 0, 1);

            _prompt = new TextBox();
            _prompt.Multiline = true;
            _prompt.Dock = DockStyle.Fill;
            _prompt.BackColor = Surface;
            _prompt.ForeColor = TextPrimary;
            _prompt.BorderStyle = BorderStyle.FixedSingle;
            _prompt.ScrollBars = ScrollBars.Vertical;
            _prompt.Text = "描述要创建或修改的机械零件...";
            root.Controls.Add(_prompt, 0, 2);

            FlowLayoutPanel sourceActions = new FlowLayoutPanel();
            sourceActions.Dock = DockStyle.Fill;
            sourceActions.FlowDirection = FlowDirection.LeftToRight;
            sourceActions.WrapContents = true;
            Button textSource = SecondaryButton("文本设计");
            Button pdfSource = SecondaryButton("选择 PDF");
            Button cadSource = SecondaryButton("选择 CAD");
            textSource.Click += delegate { SelectTextSource(); };
            pdfSource.Click += delegate { SelectFileSource("pdf"); };
            cadSource.Click += delegate { SelectFileSource("cad_file"); };
            sourceActions.Controls.Add(textSource);
            sourceActions.Controls.Add(pdfSource);
            sourceActions.Controls.Add(cadSource);
            root.Controls.Add(sourceActions, 0, 3);

            _plan = RichBox();
            _plan.Text = "计划将在这里显示。确认前不会启动 CAD 软件。";
            root.Controls.Add(_plan, 0, 4);

            TableLayoutPanel status = new TableLayoutPanel();
            status.Dock = DockStyle.Fill;
            status.ColumnCount = 2;
            status.RowCount = 3;
            status.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 82F));
            status.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
            status.Controls.Add(Label("任务状态", 8.5F, FontStyle.Regular, TextSecondary), 0, 0);
            status.Controls.Add(Label("当前步骤", 8.5F, FontStyle.Regular, TextSecondary), 0, 1);
            _taskStatus = Label("Idle", 9F, FontStyle.Bold, TextPrimary);
            _currentStep = Label("Waiting", 9F, FontStyle.Regular, TextPrimary);
            status.Controls.Add(_taskStatus, 1, 0);
            status.Controls.Add(_currentStep, 1, 1);
            _progress = new ProgressBar();
            _progress.Dock = DockStyle.Fill;
            _progress.Style = ProgressBarStyle.Continuous;
            status.Controls.Add(_progress, 0, 2);
            status.SetColumnSpan(_progress, 2);
            root.Controls.Add(status, 0, 5);

            _log = RichBox();
            _log.Font = new Font("Consolas", 8.5F, FontStyle.Regular, GraphicsUnit.Point);
            _log.Text = "Gateway ready check pending...";
            root.Controls.Add(_log, 0, 6);

            FlowLayoutPanel commands = new FlowLayoutPanel();
            commands.Dock = DockStyle.Fill;
            commands.FlowDirection = FlowDirection.LeftToRight;
            commands.WrapContents = true;
            _planButton = PrimaryButton("生成计划");
            _confirmButton = PrimaryButton("确认执行");
            _cancelButton = SecondaryButton("取消");
            _retryButton = SecondaryButton("重试");
            _openOutputButton = SecondaryButton("打开结果");
            _confirmButton.Enabled = false;
            _cancelButton.Enabled = false;
            _retryButton.Enabled = false;
            _openOutputButton.Enabled = false;
            _planButton.Click += async delegate { await PlanTask(); };
            _confirmButton.Click += async delegate { await ConfirmTask(); };
            _cancelButton.Click += async delegate { await CancelTask(); };
            _retryButton.Click += async delegate { await RetryTask(); };
            _openOutputButton.Click += delegate { OpenOutput(); };
            commands.Controls.Add(_planButton);
            commands.Controls.Add(_confirmButton);
            commands.Controls.Add(_cancelButton);
            commands.Controls.Add(_retryButton);
            commands.Controls.Add(_openOutputButton);
            root.Controls.Add(commands, 0, 7);

            _pollTimer = new Timer();
            _pollTimer.Interval = 650;
            _pollTimer.Tick += async delegate { await PollTask(); };
            _hostResizeTimer = new Timer();
            _hostResizeTimer.Interval = 300;
            _hostResizeTimer.Tick += delegate { ResizeToTaskPaneHost(); };
            _hostResizeTimer.Start();
            Load += async delegate { await LoadProviders(); };
        }

        private void ResizeToTaskPaneHost()
        {
            if (!IsHandleCreated)
            {
                return;
            }
            IntPtr parent = GetParent(Handle);
            if (parent == IntPtr.Zero)
            {
                return;
            }
            NativeRect rect;
            if (!GetClientRect(parent, out rect))
            {
                return;
            }
            int width = Math.Max(300, rect.Right - rect.Left);
            int height = Math.Max(420, rect.Bottom - rect.Top);
            SetWindowPos(Handle, IntPtr.Zero, 0, 0, width, height, 0x0014);
        }

        private async Task LoadProviders()
        {
            try
            {
                _gatewayProcess.EnsureRunning();
                Dictionary<string, object> response = await _gateway.GetProvidersAsync();
                _provider.Items.Clear();
                _provider.Items.Add(new ComboItem("自动选择 Provider", "auto"));
                object[] items = GatewayClient.ArrayValue(response, "providers");
                foreach (object item in items)
                {
                    Dictionary<string, object> provider = item as Dictionary<string, object>;
                    if (provider == null)
                    {
                        continue;
                    }
                    string id = GatewayClient.StringValue(provider, "id");
                    string label = GatewayClient.StringValue(provider, "label");
                    bool ready = GatewayClient.BoolValue(provider, "configured") && GatewayClient.BoolValue(provider, "available");
                    _provider.Items.Add(new ComboItem(label + (ready ? "" : " (未配置)"), id));
                }
                _provider.SelectedIndex = 0;
                _connection.Text = "Gateway ready";
                _connection.ForeColor = Accent;
                AppendLog("Connected to shared AI CAD Agent Gateway.");
            }
            catch (Exception exception)
            {
                _connection.Text = "Gateway error";
                _connection.ForeColor = Danger;
                AppendLog(exception.ToString());
            }
        }

        private async Task PlanTask()
        {
            try
            {
                SetBusy(true);
                Dictionary<string, object> request = new Dictionary<string, object>();
                request["source_type"] = _sourceType;
                request["prompt"] = _sourceType == "text" ? _prompt.Text.Trim() : _prompt.Text;
                request["source_path"] = string.IsNullOrWhiteSpace(_sourcePath) ? null : _sourcePath;
                request["stage_mode"] = SelectedValue(_taskMode);
                request["conversion_mode"] = _conversionMode;
                request["provider"] = SelectedValue(_provider);
                request["requested_outputs"] = new string[0];
                request["execute_real_skills"] = true;
                Dictionary<string, object> response = await _gateway.PlanAsync(request);
                _taskId = GatewayClient.StringValue(response, "task_id");
                _eventCursor = -1;
                RenderPlan(response);
                string status = GatewayClient.StringValue(response, "status");
                _taskStatus.Text = status;
                Dictionary<string, object> summary = GatewayClient.DictionaryValue(response, "summary");
                bool blocked = GatewayClient.BoolValue(summary, "needs_confirmation");
                _confirmButton.Enabled = status == "awaiting_confirmation" && !blocked;
                _cancelButton.Enabled = status == "awaiting_confirmation";
                _retryButton.Enabled = status == "failed";
                AppendLog("Plan created: task_id=" + _taskId);
            }
            catch (Exception exception)
            {
                _taskStatus.Text = "Planning failed";
                _taskStatus.ForeColor = Danger;
                AppendLog(exception.ToString());
            }
            finally
            {
                _planButton.Enabled = true;
            }
        }

        private async Task ConfirmTask()
        {
            if (string.IsNullOrWhiteSpace(_taskId))
            {
                return;
            }
            try
            {
                await _gateway.ConfirmAsync(_taskId);
                _taskStatus.Text = "queued";
                _confirmButton.Enabled = false;
                _cancelButton.Enabled = true;
                _progress.Value = 5;
                _pollTimer.Start();
                AppendLog("Task confirmed. CAD execution may now begin.");
            }
            catch (Exception exception)
            {
                AppendLog(exception.ToString());
            }
        }

        private async Task CancelTask()
        {
            if (string.IsNullOrWhiteSpace(_taskId))
            {
                return;
            }
            try
            {
                Dictionary<string, object> response = await _gateway.CancelAsync(_taskId);
                _taskStatus.Text = GatewayClient.StringValue(response, "status");
                AppendLog("Cancellation requested. The active COM call is allowed to finish safely.");
            }
            catch (Exception exception)
            {
                AppendLog(exception.ToString());
            }
        }

        private async Task RetryTask()
        {
            if (string.IsNullOrWhiteSpace(_taskId))
            {
                return;
            }
            try
            {
                Dictionary<string, object> response = await _gateway.RetryAsync(_taskId);
                _taskId = GatewayClient.StringValue(response, "task_id");
                _eventCursor = -1;
                RenderPlan(response);
                _taskStatus.Text = GatewayClient.StringValue(response, "status");
                _confirmButton.Enabled = _taskStatus.Text == "awaiting_confirmation";
                AppendLog("Retry plan created: task_id=" + _taskId);
            }
            catch (Exception exception)
            {
                AppendLog(exception.ToString());
            }
        }

        private async Task PollTask()
        {
            if (string.IsNullOrWhiteSpace(_taskId))
            {
                return;
            }
            try
            {
                Dictionary<string, object> eventResponse = await _gateway.GetEventsAsync(_taskId, _eventCursor);
                object[] events = GatewayClient.ArrayValue(eventResponse, "events");
                foreach (object value in events)
                {
                    Dictionary<string, object> item = value as Dictionary<string, object>;
                    if (item == null)
                    {
                        continue;
                    }
                    object index;
                    if (item.TryGetValue("index", out index))
                    {
                        _eventCursor = Convert.ToInt32(index);
                    }
                    string eventName = GatewayClient.StringValue(item, "event");
                    _currentStep.Text = eventName;
                    AppendLog(eventName);
                }
                Dictionary<string, object> task = await _gateway.GetTaskAsync(_taskId);
                string status = GatewayClient.StringValue(task, "status");
                _taskStatus.Text = status;
                _taskStatus.ForeColor = status == "success" ? Accent : status == "failed" ? Danger : TextPrimary;
                if (status == "running")
                {
                    _progress.Style = ProgressBarStyle.Marquee;
                }
                if (status == "success" || status == "failed" || status == "cancelled")
                {
                    _pollTimer.Stop();
                    _progress.Style = ProgressBarStyle.Continuous;
                    _progress.Value = status == "success" ? 100 : 0;
                    _cancelButton.Enabled = false;
                    _retryButton.Enabled = status != "success";
                    Dictionary<string, object> artifacts = GatewayClient.DictionaryValue(task, "artifacts");
                    _lastOutputPath = GatewayClient.StringValue(artifacts, "delivery_dir");
                    if (string.IsNullOrWhiteSpace(_lastOutputPath))
                    {
                        _lastOutputPath = GatewayClient.StringValue(task, "report_path");
                    }
                    _openOutputButton.Enabled = !string.IsNullOrWhiteSpace(_lastOutputPath);
                }
            }
            catch (Exception exception)
            {
                _pollTimer.Stop();
                AppendLog(exception.ToString());
            }
        }

        private void RenderPlan(Dictionary<string, object> response)
        {
            Dictionary<string, object> summary = GatewayClient.DictionaryValue(response, "summary");
            List<string> lines = new List<string>();
            lines.Add("任务类型: " + GatewayClient.StringValue(summary, "task_type"));
            lines.Add("计划执行:");
            foreach (object item in GatewayClient.ArrayValue(summary, "plan_lines"))
            {
                lines.Add("  - " + Convert.ToString(item));
            }
            if (GatewayClient.BoolValue(summary, "needs_confirmation"))
            {
                lines.Add("");
                lines.Add("当前计划被阻断:");
                lines.Add(GatewayClient.StringValue(summary, "confirmation_reason"));
            }
            lines.Add("");
            lines.Add("只有点击“确认执行”后才会调用 CAD 软件。");
            _plan.Text = string.Join(Environment.NewLine, lines.ToArray());
        }

        private void SelectTextSource()
        {
            _sourceType = "text";
            _sourcePath = string.Empty;
            _prompt.Text = string.Empty;
            _taskMode.Enabled = true;
        }

        private void SelectFileSource(string type)
        {
            using (OpenFileDialog dialog = new OpenFileDialog())
            {
                if (type == "pdf")
                {
                    dialog.Filter = "PDF engineering drawings (*.pdf)|*.pdf";
                }
                else
                {
                    dialog.Filter = "CAD files|*.dwg;*.dxf;*.dwt;*.step;*.stp;*.iges;*.igs;*.stl;*.x_t;*.x_b;*.sldprt;*.sldasm;*.slddrw|All files (*.*)|*.*";
                }
                if (dialog.ShowDialog(this) != DialogResult.OK)
                {
                    return;
                }
                _sourceType = type;
                _sourcePath = dialog.FileName;
                _prompt.Text = dialog.FileName;
                _taskMode.Enabled = false;
                AppendLog("Selected " + type + ": " + dialog.FileName);
            }
        }

        private void OpenOutput()
        {
            if (string.IsNullOrWhiteSpace(_lastOutputPath))
            {
                return;
            }
            string path = _lastOutputPath;
            if (File.Exists(path))
            {
                Process.Start("explorer.exe", "/select,\"" + path + "\"");
            }
            else if (Directory.Exists(path))
            {
                Process.Start("explorer.exe", "\"" + path + "\"");
            }
        }

        private void SetBusy(bool busy)
        {
            _planButton.Enabled = !busy;
            _confirmButton.Enabled = false;
            _taskStatus.Text = busy ? "planning" : _taskStatus.Text;
        }

        private void AppendLog(string text)
        {
            if (_log.TextLength > 0)
            {
                _log.AppendText(Environment.NewLine);
            }
            _log.AppendText(DateTime.Now.ToString("HH:mm:ss") + "  " + text);
            _log.SelectionStart = _log.TextLength;
            _log.ScrollToCaret();
        }

        private static string SelectedValue(ComboBox combo)
        {
            ComboItem item = combo.SelectedItem as ComboItem;
            return item == null ? "auto" : item.Value;
        }

        private static ComboBox Combo()
        {
            ComboBox combo = new ComboBox();
            combo.Dock = DockStyle.Fill;
            combo.DropDownStyle = ComboBoxStyle.DropDownList;
            combo.FlatStyle = FlatStyle.Flat;
            combo.BackColor = SurfaceRaised;
            combo.ForeColor = TextPrimary;
            return combo;
        }

        private static RichTextBox RichBox()
        {
            RichTextBox box = new RichTextBox();
            box.Dock = DockStyle.Fill;
            box.ReadOnly = true;
            box.BorderStyle = BorderStyle.FixedSingle;
            box.BackColor = Surface;
            box.ForeColor = TextPrimary;
            box.DetectUrls = false;
            return box;
        }

        private static Label Label(string text, float size, FontStyle style, Color color)
        {
            Label label = new Label();
            label.Text = text;
            label.Font = new Font("Microsoft YaHei UI", size, style, GraphicsUnit.Point);
            label.ForeColor = color;
            label.BackColor = Color.Transparent;
            label.TextAlign = ContentAlignment.MiddleLeft;
            label.Dock = DockStyle.Fill;
            return label;
        }

        private static Button PrimaryButton(string text)
        {
            Button button = SecondaryButton(text);
            button.BackColor = Accent;
            button.ForeColor = Color.FromArgb(6, 26, 24);
            button.Font = new Font("Microsoft YaHei UI", 8.5F, FontStyle.Bold, GraphicsUnit.Point);
            return button;
        }

        private static Button SecondaryButton(string text)
        {
            Button button = new Button();
            button.Text = text;
            button.AutoSize = true;
            button.Height = 30;
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderColor = Border;
            button.FlatAppearance.BorderSize = 1;
            button.BackColor = SurfaceRaised;
            button.ForeColor = TextPrimary;
            button.Margin = new Padding(0, 2, 6, 2);
            return button;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _pollTimer.Stop();
                _pollTimer.Dispose();
                _hostResizeTimer.Stop();
                _hostResizeTimer.Dispose();
                _gateway.Dispose();
            }
            base.Dispose(disposing);
        }

        private sealed class ComboItem
        {
            public ComboItem(string text, string value)
            {
                Text = text;
                Value = value;
            }

            public string Text { get; private set; }
            public string Value { get; private set; }
            public override string ToString() { return Text; }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeRect
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [DllImport("user32.dll")]
        private static extern IntPtr GetParent(IntPtr windowHandle);

        [DllImport("user32.dll")]
        private static extern bool GetClientRect(IntPtr windowHandle, out NativeRect rect);

        [DllImport("user32.dll")]
        private static extern bool SetWindowPos(
            IntPtr windowHandle,
            IntPtr insertAfter,
            int x,
            int y,
            int width,
            int height,
            uint flags);
    }
}
