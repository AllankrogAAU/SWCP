FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY apps/SWCP.Contracts/SWCP.Contracts.csproj apps/SWCP.Contracts/
COPY apps/apiGateway/api.csproj apps/apiGateway/
RUN dotnet restore apps/apiGateway/api.csproj

COPY apps/SWCP.Contracts/ apps/SWCP.Contracts/
COPY apps/apiGateway/ apps/apiGateway/
WORKDIR /src/apps/apiGateway
RUN dotnet publish api.csproj -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS final
WORKDIR /app
COPY --from=build /app/publish .

EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "api.dll"]