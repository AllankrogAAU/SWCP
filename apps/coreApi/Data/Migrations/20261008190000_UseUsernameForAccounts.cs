using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261008190000_UseUsernameForAccounts")]
public partial class UseUsernameForAccounts : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropIndex(
            name: "IX_Submissions_Status_UpdatedAtUtc",
            table: "Submissions");
        migrationBuilder.CreateIndex(
            name: "IX_Submissions_Status_UpdatedAtUtc",
            table: "Submissions",
            columns: new[] { "Status", "UpdatedAtUtc" },
            filter: "\"Status\" IN ('PENDING', 'SANDBOX_QUEUED', 'SANDBOX_PROCESSING', 'LLM_QUEUED', 'LLM_PROCESSING')");
        migrationBuilder.RenameIndex(
            name: "IX_Users_Email",
            table: "Users",
            newName: "IX_Users_Username");
        migrationBuilder.RenameColumn(
            name: "Email",
            table: "Users",
            newName: "Username");
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropIndex(
            name: "IX_Submissions_Status_UpdatedAtUtc",
            table: "Submissions");
        migrationBuilder.CreateIndex(
            name: "IX_Submissions_Status_UpdatedAtUtc",
            table: "Submissions",
            columns: new[] { "Status", "UpdatedAtUtc" },
            filter: "\"Status\" IN ('PENDING', 'SANDBOX_QUEUED', 'LLM_QUEUED')");
        migrationBuilder.RenameColumn(
            name: "Username",
            table: "Users",
            newName: "Email");
        migrationBuilder.RenameIndex(
            name: "IX_Users_Username",
            table: "Users",
            newName: "IX_Users_Email");
    }
}