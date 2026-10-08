using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261008200000_AddSubmissionActionAndTaskVerdict")]
public partial class AddSubmissionActionAndTaskVerdict : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "Action",
            table: "Submissions",
            type: "character varying(16)",
            maxLength: 16,
            nullable: false,
            defaultValue: "submit");

        migrationBuilder.AddColumn<bool>(
            name: "TaskSolved",
            table: "Submissions",
            type: "boolean",
            nullable: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(name: "Action", table: "Submissions");
        migrationBuilder.DropColumn(name: "TaskSolved", table: "Submissions");
    }
}