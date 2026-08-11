using System;
using System.Collections;
using System.Collections.Generic;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace AICADAgent.SolidWorks
{
    public sealed class GatewayClient : IDisposable
    {
        private readonly HttpClient _http;
        private readonly JavaScriptSerializer _json;

        public GatewayClient(string baseUrl, string token)
        {
            _http = new HttpClient();
            _http.BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/");
            _http.Timeout = TimeSpan.FromSeconds(120);
            _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
            _json = new JavaScriptSerializer();
            _json.MaxJsonLength = int.MaxValue;
        }

        public Task<Dictionary<string, object>> GetProvidersAsync()
        {
            return GetAsync("v1/providers");
        }

        public Task<Dictionary<string, object>> PlanAsync(Dictionary<string, object> request)
        {
            return PostAsync("v1/tasks/plan", request);
        }

        public Task<Dictionary<string, object>> ConfirmAsync(string taskId)
        {
            return PostAsync("v1/tasks/" + taskId + "/confirm", new Dictionary<string, object>());
        }

        public Task<Dictionary<string, object>> CancelAsync(string taskId)
        {
            return PostAsync("v1/tasks/" + taskId + "/cancel", new Dictionary<string, object>());
        }

        public Task<Dictionary<string, object>> RetryAsync(string taskId)
        {
            return PostAsync("v1/tasks/" + taskId + "/retry", new Dictionary<string, object>());
        }

        public Task<Dictionary<string, object>> GetTaskAsync(string taskId)
        {
            return GetAsync("v1/tasks/" + taskId);
        }

        public Task<Dictionary<string, object>> GetEventsAsync(string taskId, int after)
        {
            return GetAsync("v1/tasks/" + taskId + "/events?after=" + after.ToString());
        }

        private async Task<Dictionary<string, object>> GetAsync(string path)
        {
            HttpResponseMessage response = await _http.GetAsync(path);
            return await ReadResponse(response);
        }

        private async Task<Dictionary<string, object>> PostAsync(string path, Dictionary<string, object> payload)
        {
            string content = _json.Serialize(payload);
            HttpResponseMessage response = await _http.PostAsync(path, new StringContent(content, Encoding.UTF8, "application/json"));
            return await ReadResponse(response);
        }

        private async Task<Dictionary<string, object>> ReadResponse(HttpResponseMessage response)
        {
            string content = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
            {
                throw new InvalidOperationException(string.Format("Gateway HTTP {0}: {1}", (int)response.StatusCode, content));
            }
            object decoded = _json.DeserializeObject(content);
            Dictionary<string, object> dictionary = decoded as Dictionary<string, object>;
            if (dictionary == null)
            {
                throw new InvalidOperationException("Gateway returned an invalid JSON object.");
            }
            return dictionary;
        }

        public static Dictionary<string, object> DictionaryValue(Dictionary<string, object> data, string key)
        {
            object value;
            if (!data.TryGetValue(key, out value))
            {
                return new Dictionary<string, object>();
            }
            return value as Dictionary<string, object> ?? new Dictionary<string, object>();
        }

        public static object[] ArrayValue(Dictionary<string, object> data, string key)
        {
            object value;
            if (!data.TryGetValue(key, out value))
            {
                return new object[0];
            }
            object[] array = value as object[];
            if (array != null)
            {
                return array;
            }
            ArrayList list = value as ArrayList;
            return list == null ? new object[0] : list.ToArray();
        }

        public static string StringValue(Dictionary<string, object> data, string key)
        {
            object value;
            return data.TryGetValue(key, out value) && value != null ? Convert.ToString(value) : string.Empty;
        }

        public static bool BoolValue(Dictionary<string, object> data, string key)
        {
            object value;
            return data.TryGetValue(key, out value) && value != null && Convert.ToBoolean(value);
        }

        public void Dispose()
        {
            _http.Dispose();
        }
    }
}
