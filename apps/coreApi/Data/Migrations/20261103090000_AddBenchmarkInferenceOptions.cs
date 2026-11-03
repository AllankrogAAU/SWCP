using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261103090000_AddBenchmarkInferenceOptions")]
public partial class AddBenchmarkInferenceOptions : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "ContextMode",
            table: "Submissions",
            type: "character varying(32)",
            maxLength: 32,
            nullable: false,
            defaultValue: "with_context_transfer");

        migrationBuilder.AddColumn<string>(
            name: "AzureModelKey",
            table: "Submissions",
            type: "character varying(64)",
            maxLength: 64,
            nullable: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "ContextMode",
            table: "Submissions");

        migrationBuilder.DropColumn(
            name: "AzureModelKey",
            table: "Submissions");
    }
}
