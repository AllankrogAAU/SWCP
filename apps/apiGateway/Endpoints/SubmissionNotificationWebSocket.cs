using System.Net.WebSockets;
using System.Text.Json;
using NATS.Client.Core;
using SWCP.Contracts;

namespace api.Endpoints;

public static class SubmissionNotificationWebSocket
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    public static IEndpointRouteBuilder MapSubmissionNotificationWebSocket(this IEndpointRouteBuilder endpoints)
    {
        endpoints.MapGet("/ws/submissions/{submissionId:guid}", HandleAsync)
            .WithName("StreamSubmissionNotifications")
            .WithTags("Submission notifications")
            .RequireAuthorization();
        return endpoints;
    }

    private static async Task HandleAsync(
        Guid submissionId,
        HttpContext context,
        NatsConnection nats,
        IHttpClientFactory clients,
        CancellationToken requestAborted)
    {
        if (!context.WebSockets.IsWebSocketRequest)
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }

        if (!context.WebSockets.WebSocketRequestedProtocols.Contains("swcp"))
        {
            context.Response.StatusCode = StatusCodes.Status400BadRequest;
            return;
        }

        using var authorizationRequest = new HttpRequestMessage(
            HttpMethod.Get,
            $"/api/submissions/{submissionId:D}");
        if (context.Request.Headers.TryGetValue("Authorization", out var authorization))
        {
            authorizationRequest.Headers.TryAddWithoutValidation("Authorization", authorization.ToString());
        }
        else
        {
            var bearerProtocol = context.WebSockets.WebSocketRequestedProtocols
                .FirstOrDefault(protocol => protocol.StartsWith("bearer.", StringComparison.Ordinal));
            if (bearerProtocol is not null)
            {
                authorizationRequest.Headers.TryAddWithoutValidation(
                    "Authorization",
                    $"Bearer {bearerProtocol["bearer.".Length..]}");
            }
        }

        using var authorizationResponse = await clients.CreateClient("CoreApi")
            .SendAsync(authorizationRequest, requestAborted);
        if (!authorizationResponse.IsSuccessStatusCode)
        {
            context.Response.StatusCode = (int)authorizationResponse.StatusCode;
            return;
        }

        using var socket = await context.WebSockets.AcceptWebSocketAsync("swcp");
        using var socketLifetime = CancellationTokenSource.CreateLinkedTokenSource(requestAborted);
        var sendTask = ForwardNotificationsAsync(socket, nats, submissionId, socketLifetime.Token);
        var receiveTask = WaitForClientCloseAsync(socket, socketLifetime.Token);
        await Task.WhenAny(sendTask, receiveTask);
        await socketLifetime.CancelAsync();

        try
        {
            await Task.WhenAll(sendTask, receiveTask);
        }
        catch (OperationCanceledException) when (socketLifetime.IsCancellationRequested)
        {
        }

        if (socket.State is WebSocketState.Open or WebSocketState.CloseReceived)
        {
            await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "Submission stream closed", CancellationToken.None);
        }
    }

    private static async Task ForwardNotificationsAsync(
        WebSocket socket,
        NatsConnection nats,
        Guid submissionId,
        CancellationToken cancellationToken)
    {
        await foreach (var notification in nats.SubscribeAsync<SubmissionNotification>(
            Messaging.NotificationSubject(submissionId),
            cancellationToken: cancellationToken))
        {
            var data = notification.Data;
            if (data is null)
            {
                continue;
            }

            var payload = JsonSerializer.SerializeToUtf8Bytes(data, JsonOptions);
            await socket.SendAsync(payload, WebSocketMessageType.Text, true, cancellationToken);
            if (data.EventType is "COMPLETED" or "FAILED" or "completed" or "failed")
            {
                return;
            }
        }
    }

    private static async Task WaitForClientCloseAsync(WebSocket socket, CancellationToken cancellationToken)
    {
        var buffer = new byte[1024];
        while (socket.State == WebSocketState.Open && !cancellationToken.IsCancellationRequested)
        {
            var result = await socket.ReceiveAsync(buffer, cancellationToken);
            if (result.MessageType == WebSocketMessageType.Close)
            {
                return;
            }
        }
    }
}
