FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY ["apps/api/api/api.csproj", "apps/api/api/"]
RUN dotnet restore "apps/api/api/api.csproj"

COPY apps/api/api/. apps/api/api/
WORKDIR /src/apps/api/api
RUN dotnet publish -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS final
WORKDIR /app
COPY --from=build /app/publish .

EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "api.dll"]
