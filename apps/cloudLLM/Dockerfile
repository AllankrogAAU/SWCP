FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY ["apps/cloudLLM/cloudLLM.csproj", "apps/cloudLLM/"]
RUN dotnet restore "apps/cloudLLM/cloudLLM.csproj"

COPY apps/cloudLLM/. apps/cloudLLM/
WORKDIR /src/apps/cloudLLM
RUN dotnet publish -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS final
WORKDIR /app
COPY --from=build /app/publish .

EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "cloudLLM.dll"]
