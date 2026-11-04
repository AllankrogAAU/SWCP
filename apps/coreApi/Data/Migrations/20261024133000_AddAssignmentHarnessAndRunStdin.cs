using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261024133000_AddAssignmentHarnessAndRunStdin")]
public partial class AddAssignmentHarnessAndRunStdin : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "HarnessSource",
            table: "Assignments",
            type: "text",
            nullable: true);

        migrationBuilder.AddColumn<string>(
            name: "StandardInput",
            table: "Submissions",
            type: "text",
            nullable: false,
            defaultValue: "");
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "HarnessSource",
            table: "Assignments");

        migrationBuilder.DropColumn(
            name: "StandardInput",
            table: "Submissions");
    }
}
