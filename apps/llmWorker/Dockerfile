FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY apps/SWCP.Contracts/SWCP.Contracts.csproj apps/SWCP.Contracts/
COPY apps/llmWorker/llmWorker.csproj apps/llmWorker/
RUN dotnet restore apps/llmWorker/llmWorker.csproj

COPY apps/SWCP.Contracts/ apps/SWCP.Contracts/
COPY apps/llmWorker/ apps/llmWorker/
WORKDIR /src/apps/llmWorker
RUN dotnet publish llmWorker.csproj -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/runtime:10.0 AS final
WORKDIR /app
COPY --from=build /app/publish .

ENTRYPOINT ["dotnet", "llmWorker.dll"]
