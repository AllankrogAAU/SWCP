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

        var finishedTask = await Task.WhenAny(sendTask, receiveTask);
        if (finishedTask == sendTask)
        {
            // Server finished sending terminal notification ("COMPLETED" / "FAILED").
            // Initiate graceful WebSocket close handshake so client receives close frame instead of TCP RST.
            if (socket.State == WebSocketState.Open)
            {
                try
                {
                    await socket.CloseOutputAsync(WebSocketCloseStatus.NormalClosure, "Submission stream closed", CancellationToken.None);
                }
                catch
                {
                    // Ignore socket write failures during shutdown
                }
            }

            // Give the client up to 5 seconds to acknowledge the close frame
            using var timeoutCts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            using var gracefulCts = CancellationTokenSource.CreateLinkedTokenSource(timeoutCts.Token, requestAborted);
            try
            {
                await receiveTask.WaitAsync(gracefulCts.Token);
            }
            catch (OperationCanceledException)
            {
                // Timeout or requestAborted while waiting for client ack
            }
            catch
            {
            }
        }
        else
        {
            // Client closed connection first or connection dropped
            if (socket.State is WebSocketState.CloseReceived or WebSocketState.Open)
            {
                try
                {
                    await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "Submission stream closed", CancellationToken.None);
                }
                catch
                {
                }
            }
        }

        // Cancel any remaining background task (e.g. NATS subscription if client closed early)
        await socketLifetime.CancelAsync();

        try
        {
            await Task.WhenAll(sendTask, receiveTask);
        }
        catch (OperationCanceledException)
        {
        }
        catch
        {
        }
    }

    private static async Task ForwardNotificationsAsync(
        WebSocket socket,
        NatsConnection nats,
        Guid submissionId,
        CancellationToken cancellationToken)
    {
        try
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
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (WebSocketException)
        {
        }
    }

    private static async Task WaitForClientCloseAsync(WebSocket socket, CancellationToken cancellationToken)
    {
        var buffer = new byte[1024];
        while ((socket.State == WebSocketState.Open || socket.State == WebSocketState.CloseSent) && !cancellationToken.IsCancellationRequested)
        {
            WebSocketReceiveResult result;
            try
            {
                result = await socket.ReceiveAsync(buffer, cancellationToken);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (WebSocketException)
            {
                return;
            }

            if (result.MessageType == WebSocketMessageType.Close)
            {
                return;
            }
        }
    }
}
