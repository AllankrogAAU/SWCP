FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY ["apps/api/api.csproj", "apps/api/"]
RUN dotnet restore "apps/api/api.csproj"

COPY apps/api/. apps/api/
WORKDIR /src/apps/api
RUN dotnet publish "api.csproj" -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS final
WORKDIR /app
COPY --from=build /app/publish .

EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "api.dll"]