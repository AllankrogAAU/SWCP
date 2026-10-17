FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY apps/SWCP.Contracts/SWCP.Contracts.csproj apps/SWCP.Contracts/
COPY apps/coreApi/coreApi.csproj apps/coreApi/
RUN dotnet restore apps/coreApi/coreApi.csproj

COPY apps/SWCP.Contracts/ apps/SWCP.Contracts/
COPY apps/coreApi/ apps/coreApi/
WORKDIR /src/apps/coreApi
RUN dotnet publish coreApi.csproj -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS final
WORKDIR /app
RUN apt-get update \
	&& apt-get install -y --no-install-recommends curl \
	&& rm -rf /var/lib/apt/lists/*
COPY --from=build /app/publish .

EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "coreApi.dll"]