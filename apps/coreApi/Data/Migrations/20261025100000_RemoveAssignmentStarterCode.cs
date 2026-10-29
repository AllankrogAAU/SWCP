using coreApi.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace coreApi.Data.Migrations;

[DbContext(typeof(CoreDbContext))]
[Migration("20261025100000_RemoveAssignmentStarterCode")]
public partial class RemoveAssignmentStarterCode : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "StarterCode",
            table: "Assignments");
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "StarterCode",
            table: "Assignments",
            type: "text",
            nullable: true);
    }
}
